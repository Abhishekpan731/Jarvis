# JARVIS — API Conventions

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

Base path: `/api/v1`. All request and response bodies are JSON. All timestamps
are ISO 8601 in UTC.

## Authentication

Protected endpoints require `Authorization: Bearer <access token>`. The refresh
token is an HttpOnly cookie named `jarvis_refresh` and is never accepted in a
header or body.

## Errors

Every error uses the same shape:

```json
{
  "error": {
    "code": "task_not_found",
    "message": "No task with that id exists for this account."
  }
}
```

| HTTP | Meaning |
|---|---|
| 400 | Malformed request |
| 401 | Missing or invalid credentials |
| 403 | Authenticated but not allowed |
| 404 | Not found, or not owned by the caller — these are intentionally identical |
| 409 | Conflict, such as an illegal state transition |
| 422 | Validation failed; `message` lists the fields |
| 429 | Rate limited |
| 503 | A required dependency is unavailable |

A resource that belongs to someone else returns 404, not 403, so the API does
not reveal that it exists.

## Pagination

List endpoints take `limit` (default 20, maximum 100) and `cursor`. The
response carries `total`. There is no `next_cursor`. Conversation and message
lists page with `limit` and `offset`. Conversation `limit` is capped at 100;
message `limit` is capped at 200. Task and notification lists take `limit`
only, capped at 50, and do not page.

## Identifiers

All identifiers are UUIDs except agent IDs, which are stable slugs such as
`research` and `calendar`.

## Phase 1 endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness. Returns `{ status, ai, speech }`. `ai` is `{ configured, provider, model }`. `speech` is `{ stt_configured, tts_configured, stt_provider, tts_provider, locales, browser_capture }`. |
| POST | `/api/v1/auth/register` | Create an account |
| POST | `/api/v1/auth/login` | Issue tokens |
| POST | `/api/v1/auth/refresh` | Rotate tokens |
| POST | `/api/v1/auth/logout` | Revoke the session |
| GET | `/api/v1/auth/session` | The current user |
| POST | `/api/v1/auth/password` | Change the password and revoke every session |
| DELETE | `/api/v1/auth/account` | Soft-delete the account and revoke every session |
| POST | `/api/v1/tasks` | Create a task. 202 when the plan needs approval, otherwise 201 |
| GET | `/api/v1/tasks` | List the caller's tasks. `limit` only, capped at 50 |
| GET | `/api/v1/tasks/{id}` | Task detail |
| GET | `/api/v1/tasks/{id}/jobs` | Jobs for the task |
| POST | `/api/v1/tasks/{id}/cancel` | Cancel. 409 `not_cancellable` when already terminal |
| POST | `/api/v1/tasks/{id}/retry` | Retry a failed task. 409 `not_retryable` otherwise |

There is no `/api/v1/agents` route and no `/api/v1/tasks/{id}/transition`
route. Agent ids live in `jarvis_shared.agents` (18 specs). A caller does not
pick an arbitrary next state; cancel and retry are the only transitions the
API exposes, and both go through `apply_transition`.
