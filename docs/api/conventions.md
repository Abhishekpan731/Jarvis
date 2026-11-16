# JARVIS API conventions

**Author**: Abhishek Pandey  
**Last Updated**: 2026-11-16

The API prefix is `/api/v1`. API request and response bodies are JSON unless
a route explicitly returns server-sent events (SSE); FastAPI docs routes serve
HTML. Timestamps are ISO 8601 in UTC. This is the registered API surface, not a phase-specific subset. Route
registration is in [`app/main`](../../apps/api/app/main/__init__.py).

## Authentication

Protected user routes require `Authorization: Bearer <access token>`. The
refresh token is an HttpOnly `jarvis_refresh` cookie, used by
`POST /api/v1/auth/refresh`; it is not accepted in a header or body. The admin
overview uses the configured operator token as a Bearer token, not a user
access token. `GET /api/v1/health` and FastAPI's generated schema/docs routes do
not require user authentication.

## Errors and status codes

Routes raise `HTTPException` with their own status codes. The global handler
passes dictionary details through unchanged (routes commonly use this shape)
and wraps non-dictionary details as `{"error":{"code":"error","message":"..."}}`.
FastAPI request-validation errors are normalized to HTTP 422 with
`{"error":{"code":"invalid_request","message":"Invalid fields: ..."}}`.
Some handlers perform their own validation and return route-specific codes or
statuses instead (for example, task request validation is HTTP 400).

```json
{"error":{"code":"not_found","message":"No task with that id exists for this account."}}
```

Successful responses default to HTTP 200 unless the route sets another status.
HTTP 201 is set for account registration, conversation creation and
non-streaming message submission, memory creation, device registration, and
reminder creation. `POST /api/v1/assistant/commands` sets HTTP 202. Task routes
have an implementation caveat: task creation computes 201/202, a cancel
request for a running job computes 202, and retry computes 202, but these values
are put in the JSON `http_status` field; the routes do not set the HTTP response
status, so the actual response remains the FastAPI default 200. An idempotent task replay returns HTTP 200;
a reused key with a different task request returns HTTP 409.

A resource owned by another user is generally reported as 404, not 403, to
avoid revealing its existence. Authentication/authorization and other errors
retain the status selected by the route; status meanings are not a universal
mapping for every endpoint.

## Registered API routes

| Area | Methods and paths | Behavior |
|---|---|---|
| Health | `GET /api/v1/health` | Returns process-level status and AI/speech configuration; see below. |
| Auth | `POST /api/v1/auth/register`; `POST /api/v1/auth/login`; `POST /api/v1/auth/refresh`; `POST /api/v1/auth/logout`; `GET /api/v1/auth/session`; `POST /api/v1/auth/password`; `DELETE /api/v1/auth/account` | Register accepts `email`, `password`, optional `display_name`; login accepts `email` and `password`; password change accepts `current_password` and `new_password`. Refresh rotates the cookie session; account deletion is soft. Register returns 201; login is throttled. |
| Conversations | `GET`, `POST /api/v1/conversations`; `GET`, `PATCH`, `DELETE /api/v1/conversations/{conversation_id}`; `GET /api/v1/conversations/{conversation_id}/messages`; `POST /api/v1/conversations/{conversation_id}/messages`; `POST /api/v1/conversations/{conversation_id}/stream` | CRUD, messages, and streamed replies. Request fields, limits, responses, and SSE events are in [Conversation API](conversations.md). |
| Memory | `GET /api/v1/memory`; `POST /api/v1/memory`; `PATCH /api/v1/memory/{memory_id}`; `DELETE /api/v1/memory/{memory_id}` | Create accepts `category`, `content`, optional `privacy_class` (default `normal`); patch accepts `content`, `active`, `privacy_class`. Categories are profile, preferences, work, projects, goals, knowledge, decisions, routines, summaries, instructions; privacy is `normal` or `sensitive`. List supports `q` and returns at most 100, with no offset/cursor. Create returns 201. |
| Tasks | `GET`, `POST /api/v1/tasks`; `GET /api/v1/tasks/{task_id}`; `GET /api/v1/tasks/{task_id}/jobs`; `POST /api/v1/tasks/{task_id}/cancel`; `POST /api/v1/tasks/{task_id}/retry` | Create/list tasks, inspect jobs, cancel, or retry. No approve/reject or arbitrary-transition route exists. Approval-waiting tasks have no job. Creation request and idempotency semantics are below. |
| Assistant | `POST /api/v1/assistant/commands` | Accepts `command`, `source` (default `api`), optional `device_id` and `conversation_id`, `input_type` (default `text`), and `locale` (default `en`). Sources: `siri`, `google_assistant`, `web`, `desktop`, `api`; input types: `voice`, `text`. Returns HTTP 202 and creates a conversation when `conversation_id` is omitted. `Idempotency-Key` replays the stored result. `device_id` is attribution, not authentication. |
| Devices | `GET /api/v1/devices`; `POST /api/v1/devices`; `DELETE /api/v1/devices/{device_id}` | Register accepts `platform` (`ios`, `android`, `web`, `desktop`, `api`) and `label`; list and revoke the caller's devices. Create returns 201. |
| Sync | `GET /api/v1/sync` | Read the caller's durable events using `after` and `limit`; returns the next `cursor`. |
| Voice | `GET /api/v1/voice/session`; `POST /api/v1/voice/speak` | Session accepts optional `locale`; speak accepts `text` and optional `locale`. Reports browser voice settings and returns speakable text; does not receive audio or synthesize audio. |
| Integrations | `GET /api/v1/integrations`; `POST /api/v1/integrations/{provider}/connect`; `POST /api/v1/integrations/google/callback`; `DELETE /api/v1/integrations/{provider}` | List, begin/finish OAuth (callback accepts `code`, `state`, optional `error_code`), or disconnect a provider. Current provider IDs are `gmail` and `google_calendar`; provider path values are names, not UUIDs. |
| Notifications | `GET /api/v1/notifications`; `POST /api/v1/notifications/{note_id}/read`; `GET`, `PUT /api/v1/notifications/schedule`; `GET`, `POST /api/v1/notifications/reminders`; `DELETE /api/v1/notifications/reminders/{reminder_id}` | Schedule requires `hour`; `minute` defaults to 0, `timezone` to `UTC`, and `enabled` to true. Reminder creation requires `name` and `hour`; `body` defaults empty, `minute` to 0, `timezone` to `UTC`, and new reminders are enabled. List/read notifications and manage briefing/reminders; create returns 201. This API does not claim to send a push notification. |
| Admin | `GET /api/v1/admin/overview` | Operator-only aggregate queue/usage view; response has `content: null`. |

### Task creation

`POST /api/v1/tasks` accepts this JSON body:

```json
{"instruction":"Ask the research agent to summarize recent climate policy","priority":0}
```

`instruction` is required (1–20,000 characters); `priority` defaults to 0
and must be between -10 and 10. `Idempotency-Key` is optional: same key and
same request replays the existing task with HTTP 200; same key with a different
request returns HTTP 409 `idempotency_conflict`. A new plan that needs approval
creates a waiting task without a job. There is no endpoint to approve or reject
that task. The response's `http_status` field reports the intended 201 or 202,
but the actual HTTP status is 200 as noted above.

## Pagination and bounded lists

| Route | Query parameters and behavior |
|---|---|
| `GET /api/v1/conversations` | `limit` default 20, capped at 100; `offset` default 0; response includes `total`. |
| `GET /api/v1/conversations/{conversation_id}/messages` | `limit` default 50, capped at 200; `offset` default 0; response has no total. |
| `GET /api/v1/tasks` | `limit` default 20, capped at 50; no offset or cursor. |
| `GET /api/v1/notifications` | `limit` default 20, capped at 50; no offset or cursor. |
| `GET /api/v1/memory` | Optional `q`; fixed maximum of 100 results; no offset or cursor. `q` matches content case-insensitively or category exactly. |
| `GET /api/v1/sync` | `after` default 0 and `limit` default 100, capped at 500; pass the returned `cursor` as the next `after`. No total is returned. |

Other collection routes are unpaginated.

## Health response

An unconfigured instance returns a body shaped like this (configured provider
fields become strings when set):

```json
{
  "status": "ok",
  "ai": {"configured": false, "provider": null, "model": null},
  "speech": {
    "stt_configured": false,
    "tts_configured": false,
    "stt_provider": null,
    "tts_provider": null,
    "locales": ["en", "hi"],
    "browser_capture": true
  }
}
```

The provider fields reflect configuration, not a live provider check. This
handler does not check the database or worker; do not use it as a
database/worker readiness check.

## FastAPI docs and OpenAPI

FastAPI's default `/docs`, `/redoc`, and `/openapi.json` routes are registered.
The API middleware sets `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`;
the Swagger UI or ReDoc pages may therefore fail to load/render their assets.
This is a code-based warning, not a browser test.

## Identifiers

Most resource `{..._id}` path parameters are UUIDs. Agent IDs and integration
provider values are names/slugs, not UUIDs. Use each route's documented value
type; do not assume every identifier is a UUID.
