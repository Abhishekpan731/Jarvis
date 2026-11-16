# JARVIS — Implementation Roadmap

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

Each phase ends with working, tested software and updated documentation. A
phase is not done because code was written; it is done because its tests pass.
The verification column records checks against this checkout; passing tests or
code review do not establish that a feature is available in a live deployment.

## Current implementation status

Audited 2026-11-15 against the working tree, not against earlier notes. A row
is verified only when a command was run and its output observed. Verification
is evidence about this checkout and the stated test environment, not proof of a
live or production deployment.

| Feature         | Existing implementation                                                                                                                                                                                                                                                        | Verified                                                                                                                | Remaining work                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Authentication  | Register, login, logout, API refresh with rotation and family revocation, session lookup, password change, soft account deletion. Argon2id, HttpOnly refresh cookie, in-process login throttle. Password change and deletion revoke every session for the account. The web client does not call refresh or retry requests. | Yes. `tests/security/test_auth.py` and `tests/security/test_review_regressions.py`, against PostgreSQL.                                   | Web-client refresh/retry flow. Redis-backed rate limiting. Hard deletion of user data.                                                       |
| Database        | PostgreSQL 16 with pgvector; migrations `0001` through `0009` are checked in. `0009` creates the non-owner `jarvis_app` runtime role, forces RLS on tenant-owned tables, and adds `WITH CHECK` policies.                                                                 | `tests/security/test_security.py` defines coverage for fail-closed RLS, cross-tenant reads of memories/devices, worker-GUC SELECT visibility of a foreign job, and rejection of a cross-tenant task insert. This review did not verify the live DB version; the prior row recorded `0005`. | Verify deployment DBs applied `0009`; expand coverage for joined-table and non-queue worker-GUC boundaries. |
| Agents          | 18 agents in `jarvis_shared.agents` and seeded by migration `0001`.                                                                                                                                                                                                            | Yes. `select count(*) from agents` returns 18. Registry import returns 18.                                              | No idempotent re-seed command. The migration inserts once; a second run is a no-op because Alembic tracks it. |
| Task execution  | Durable PostgreSQL queue. The task helper defines legal transitions; the worker claims queued jobs with leases and fencing. High-risk tasks are saved as `waiting_for_approval` without a job. No approve/reject endpoint exists; the worker rejects that status, and cancellation returns 409 when no active job exists. Google integration steps perform limited read-only requests; other agent steps currently return a placeholder `<agent_id>: completed` result rather than performing their advertised capability. | Yes. `tests/jobs/test_durable_jobs.py` and `tests/jobs/test_worker_recovery.py`.                                                  | Implement approval decisions and a supported cancellation path for waiting tasks (the current route returns 409). No CI runs these automatically.                                                                               |
| Frontend        | Next.js app in `apps/web`: sign in, register, conversation list, streaming chat, stop control, hold-to-talk, English/Hindi switch.                                                                                                                                             | Partially. `tsc` not re-run this phase. No browser test was run.                                                        | Rename and delete exist in the API client but not in the UI. Microphone use is untested on a device.          |
| AI integration  | Provider interface, OpenAI implementation, factory that reports unconfigured instead of inventing a reply.                                                                                                                                                                     | Partially. Missing-key path verified: health reports `configured: false` and messaging returns 503 `ai_not_configured`. | No live call was made. No `OPENAI_API_KEY` is set.                                                            |
| Voice           | Browser Web Speech APIs provide push-to-talk, English and Hindi, and text fallback. A transcript is submitted to `POST /api/v1/assistant/commands` (`source: web`, `input_type: voice`). Unsupported voice locales are rejected. Typed chat is not spoken. No wake word or server-side STT/TTS provider implementation. | Yes. `tests/integrations/test_voice.py`, including owner isolation on an existing conversation. Browser capture was not exercised.   | A speech vendor, once a credential exists.                                                                    |
| Supervisor      | Plans a request, validates agents against the registry, forces approval on high-risk actions. One planner serves every source.                                                                                                                                                | Yes. `tests/assistant/test_supervisor.py`, 4 tests.                                                                               | Routing uses the model only when one is configured. Without a key it falls back to keyword matching.          |
| Command gateway | `POST /api/v1/assistant/commands` accepts a source-tagged contract (`siri`, `google_assistant`, `web`, `desktop`, `api`), normalizes it, and routes it through the shared planner. A device id is attribution, never authentication.                                           | Yes. `tests/assistant/test_assistant.py`, 4 tests, against PostgreSQL.                                                            | Not exercised from a physical Siri or Google Assistant device.                                                |
| Devices         | `devices` table with registration, per-device revocation, and last-seen tracking. Revoking a device blocks further commands attributed to it.                                                                                                                                  | Yes. `tests/assistant/test_assistant.py`.                                                                                         | No push transport (APNs/FCM). A client can poll `/api/v1/sync`; the current web UI does not.                                                    |
| Sync            | Durable `sync_events` log with cursor replay at `GET /api/v1/sync`. Scoped per user; idempotency rows are excluded from the feed.                                                                                                                                              | Yes. `tests/assistant/test_assistant.py` covers replay and cross-account isolation.                                               | No live SSE push. Replay is pull-based.                                                                       |
| Memory          | Table, CRUD API, keyword search, privacy class, active flag. Updates re-validate the privacy class.                                                                                                                                                                            | No. Endpoints exist; no test covers them.                                                                               | No embeddings are generated. The `embedding` column is empty.                                                 |
| Streaming       | SSE endpoint emitting `response.started`, `text.delta`, `response.completed`, `error`.                                                                                                                                                                                         | No. Code reviewed, not exercised against a provider.                                                                    | Needs a configured provider to verify end to end.                                                             |
| Integrations    | Gmail and Google Calendar OAuth connection flow with PKCE, single-use state, and Fernet ciphertext. Gmail reads the profile message total; Calendar reads the calendar list. No mail content, mail sending, or calendar writes are implemented. | Yes. `tests/integrations/test_integrations.py` and the email-step case in `tests/jobs/test_worker_recovery.py`. No live Google call was made.  | Configure a Google client to test live reads. No `GOOGLE_CLIENT_ID` is set.          |

### What is explicitly not done

- Semantic memory retrieval. The column exists; nothing writes it. Memory context can include active sensitive-tagged memories; no proposal-and-commit workflow exists.
- High-risk task approval decisions. Such tasks are stored without jobs; no approve/reject endpoint exists, and the worker refuses to run them.
- Wake word and local device control. Impossible under the zero-install constraint.
- Native Siri and Google Assistant invocation. Both require a native app to
  register intents; none is installed. See `docs/architecture/ASSISTANTS.md`.
- Push delivery to devices. Sync is pull-based replay, not APNs or FCM.
- Live LLM responses. No credential is configured, and none was invented.
- A live Google connection. No client id or secret is configured, so no mailbox
  or calendar was read.

## Phase 1 — Foundation (complete)

Monorepo, configuration, structured logging, PostgreSQL with migrations, the
identity tables, authentication (register, login, refresh, logout), the task
state machine with recorded transitions, agent registry, health endpoints, and
the API skeleton the UI talks to.

**Done when:** a user can register and log in; the shared task-state helper
accepts legal transitions and rejects illegal ones; application state changes
record `task_events`; and one user cannot read another's data. The API does not
expose arbitrary state transitions, and high-risk approval decisions are not
implemented.

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
LOCKED`, holds a lease it heartbeats, and reaps expired leases according to the
attempt policy. Cancellation and idempotency keys are in place. Covered by
`tests/jobs/test_durable_jobs.py` and `tests/jobs/test_worker_recovery.py`. The queue is
PostgreSQL, not Redis; Redis remains available for a future rate limiter.

## Phase 7 — Voice (done, browser path)

Push-to-talk through browser Web Speech APIs, English and Hindi, and text
fallback when the browser cannot capture. No wake word or server-side STT/TTS
provider implementation exists. See
`docs/architecture/ARCHITECTURE.md`. Covered by `tests/integrations/test_voice.py`.

## Phase 8 — Integrations

Implemented for Gmail and Google Calendar, without a live Google call. The
connection flow uses PKCE and a single-use state. Tokens are Fernet ciphertext.
The web app posts the callback, so the authorization code is not on the API
query string. Gmail reads profile totals and Calendar reads the calendar list;
there is no mail-content read, mail send, or calendar write. A command that
needs a missing connection is not queued, and a worker does not invent a read.
No client id is configured, so this is not live-tested. Covered by
`tests/integrations/test_integrations.py`.

## Phase 9 — Notifications and automations (done, in-app only)

A signed-in user pulls notification rows. The worker writes a daily briefing
from that user's open tasks, and a daily reminder the user wrote, once per
local day. The web client can show an unread row with the browser Notification
API after the user grants permission. It does not push to a device or implement
email delivery. Covered by `tests/operations/test_notifications.py`. Not
device-tested.

## Phase 10 — Security hardening (partial)

Some baseline controls are implemented and tested in
`tests/security/test_security.py`; significant gaps remain. See the high-priority
gaps in [SECURITY.md](../security/SECURITY.md).

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
