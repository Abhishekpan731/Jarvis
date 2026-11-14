# JARVIS — API Conventions

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
response carries `next_cursor`, which is null on the last page.

## Identifiers

All identifiers are UUIDs except agent IDs, which are stable slugs such as
`research` and `calendar`.

## Phase 1 endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness and dependency status |
| POST | `/api/v1/auth/register` | Create an account |
| POST | `/api/v1/auth/login` | Issue tokens |
| POST | `/api/v1/auth/refresh` | Rotate tokens |
| POST | `/api/v1/auth/logout` | Revoke the session |
| GET | `/api/v1/auth/session` | The current user |
| GET | `/api/v1/agents` | Registered agents and their capabilities |
| POST | `/api/v1/tasks` | Create a task |
| GET | `/api/v1/tasks` | List the caller's tasks |
| GET | `/api/v1/tasks/{id}` | Task detail with steps and events |
| POST | `/api/v1/tasks/{id}/transition` | Move a task to a new state |
