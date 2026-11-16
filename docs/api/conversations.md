# Conversation API

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

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
| POST | `/api/v1/conversations/{id}/messages` | `{ "content", "source"? }` | The assistant message |
| POST | `/api/v1/conversations/{id}/stream` | `{ "content", "source"? }` | `text/event-stream` |

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

event: assistant.handoff
data: {"device_assistant": "siri", "text": "No model is configured. Ask Siri on this device."}
```

`response.started` is sent before any provider call. `error` ends the stream.
`assistant.handoff` ends the stream when `source` is `siri` or
`google_assistant` and `OPENAI_API_KEY` is not set. The spoken note is also
stored as the assistant message. Other sources still get `error`.
No event carries credentials or hidden reasoning.

## Errors

| Status | Code | When |
|---|---|---|
| 401 | `unauthenticated` | Missing or expired token |
| 404 | `not_found` | No such conversation for this account |
| 422 | `invalid_request` | Body failed validation |
| 422 | `invalid_source` | `source` is not `web`, `siri`, `google_assistant`, `desktop`, or `api` |
| 503 | `ai_not_configured` | `OPENAI_API_KEY` is not set and `source` is not a device assistant |
| 503 | `provider_error` | The provider returned an error |
