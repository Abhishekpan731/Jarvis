# Conversation API

**Author**: Abhishek Pandey  
**Last Updated**: 2026-11-16

All routes require `Authorization: Bearer <access token>`. A conversation that
belongs to another account, or has been deleted, returns 404. Routes are
registered in [`app/routers/conversations`](../../apps/api/app/routers/conversations/__init__.py).

| Method and path | Request | Result |
|---|---|---|
| `POST /api/v1/conversations` | Optional `title` (default `"New conversation"`, max 200 characters). | Creates an active conversation; HTTP 201. |
| `GET /api/v1/conversations` | Query: `limit` (default 20, max 100), `offset` (default 0). | `{ "conversations": [...], "total": n }`, newest updated first. |
| `GET /api/v1/conversations/{conversation_id}` | — | Conversation record. |
| `PATCH /api/v1/conversations/{conversation_id}` | Optional `title` (max 200 characters) and/or `status`; status must be `active` or `archived`. | Updated conversation record. |
| `DELETE /api/v1/conversations/{conversation_id}` | — | Soft-deletes the conversation; `{ "deleted": true }`. |
| `GET /api/v1/conversations/{conversation_id}/messages` | Query: `limit` (default 50, max 200), `offset` (default 0). | `{ "messages": [...] }`, oldest first; no total. |
| `POST /api/v1/conversations/{conversation_id}/messages` | `content` (1–20,000 characters), optional `source`. | Stores the user message and returns the assistant message plus `task_id`, `handoff`, and `device_assistant`; HTTP 201. |
| `POST /api/v1/conversations/{conversation_id}/stream` | Same body as non-streaming message submission. | HTTP 200 `text/event-stream`; stores the user message before streaming. |

The request body for a message can be:

```json
{"content":"Summarize this conversation","source":"web"}
```

`source` is optional and, when present, must be `siri`, `google_assistant`,
`web`, `desktop`, or `api`. It is a request field; the serialized message
response does not include it.

## Stream events

Each SSE event has an `event` line and a JSON `data` line:

```text
event: response.started
data: {}

event: text.delta
data: {"text":"Hello"}

event: response.completed
data: {"text":"Hello, how can I help?"}

```

The stream can also emit:

| Event | Data | Meaning |
|---|---|---|
| `error` | `{ "code": "ai_not_configured", "message": "No model provider is configured." }` | No model is configured and the source has no device assistant to hand off to. Ends the stream. |
| `error` | `{ "code": "provider_error", "message": "..." }` | Provider failed during streaming. Ends the stream. |
| `assistant.handoff` | `{ "device_assistant": "siri", "text": "..." }` | No model is configured and the source maps to a device assistant. The note is stored as the assistant message. |

`response.started` is emitted before the provider call. Stream errors are SSE
events after the HTTP 200 response starts; they do not change the HTTP status.
The assistant reply is persisted after `response.completed`; disconnecting
before that point can leave the user message stored without the reply. No event
contains credentials or hidden reasoning.

The web stream caller attaches `Authorization: Bearer <access token>` but does
not set `credentials: "include"`; it relies on the access token rather than
sending the refresh cookie for this request.

## Errors

| Status or event | Code | When |
|---|---|---|
| HTTP 401 | `unauthenticated` / `session_revoked` | Missing, expired, or revoked user session. |
| HTTP 404 | `not_found` | Conversation does not exist, is deleted, or is not owned by the caller. |
| HTTP 422 | `invalid_request` | FastAPI request validation failed; global handler lists invalid fields. |
| HTTP 422 | `invalid_source` | `source` is not a known command source. |
| HTTP 422 | `invalid_status` | Patch `status` is not `active` or `archived`. |
| HTTP 503 | `ai_not_configured` | Non-streaming `POST /api/v1/conversations/{conversation_id}/messages`, when no model is configured and the source is not a device assistant. |
| HTTP 503 | `provider_error` | Non-streaming `POST /api/v1/conversations/{conversation_id}/messages`, when the provider fails after its retries. The user's message and an honest failure note are both persisted. |
| SSE `error` event | `ai_not_configured` / `provider_error` | Stream failure after the HTTP response has started; HTTP status remains 200. |

HTTP exceptions use the application's JSON error handler. Request-validation
errors are normalized to HTTP 422; see [API conventions](conventions.md).
