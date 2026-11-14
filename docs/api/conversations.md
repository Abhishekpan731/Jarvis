# Conversation API

All routes require `Authorization: Bearer <access token>`. A conversation that
belongs to another account returns 404.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/v1/conversations` | `{ "title": "..." }` | The conversation |
| GET | `/api/v1/conversations` | `limit`, `offset` | `{ conversations, total }` |
| GET | `/api/v1/conversations/{id}` | | The conversation |
| PATCH | `/api/v1/conversations/{id}` | `{ "title", "status" }` | Updated |
| DELETE | `/api/v1/conversations/{id}` | | `{ "deleted": true }` |
| GET | `/api/v1/conversations/{id}/messages` | `limit`, `offset` | `{ messages }` |
| POST | `/api/v1/conversations/{id}/messages` | `{ "content" }` | The assistant message |
| POST | `/api/v1/conversations/{id}/stream` | `{ "content" }` | `text/event-stream` |

## Stream events

```
event: response.started
data: {}

event: text.delta
data: {"text": "Hello"}

event: response.completed
data: {"text": "Hello, how can I help?"}

event: error
data: {"code": "ai_not_configured", "message": "No model provider is configured."}
```

`response.started` is sent before any provider call. `error` ends the stream.
No event carries credentials or hidden reasoning.

## Errors

| Status | Code | When |
|---|---|---|
| 401 | `unauthenticated` | Missing or expired token |
| 404 | `not_found` | No such conversation for this account |
| 422 | `invalid_request` | Body failed validation |
| 503 | `ai_not_configured` | `OPENAI_API_KEY` is not set |
| 503 | `provider_error` | The provider returned an error |
