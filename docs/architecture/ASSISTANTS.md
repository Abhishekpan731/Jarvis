# JARVIS — Native assistant integration

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

The API accepts `siri` and `google_assistant` as command source attribution
labels. No native iOS or Android app invokes this endpoint; those labels do not
represent installed assistant integrations. Commands submitted to the endpoint
reach the same planner (`app/ai/supervisor.py`), agent registry, and durable
queue when the plan is executable.

## What is implemented

`POST /api/v1/assistant/commands` accepts this source-tagged request contract:

```json
{
  "command": "Summarize my pending tasks",
  "source": "siri",
  "device_id": "optional-registered-device-uuid",
  "conversation_id": "optional-uuid",
  "input_type": "voice",
  "locale": "en-IN"
}
```

`source` is one of `siri`, `google_assistant`, `web`, `desktop`, `api`.
`input_type` is `voice` or `text`. The command is normalized (whitespace
collapsed, surrounding quotes removed) before planning.

The response carries `request_id`, `conversation_id`, `task_id`, `status`,
`response_text`, `voice_response`, and `requires_approval`. `voice_response` is
capped at 240 characters so a spoken reply stays short. An `Idempotency-Key`
header replays the original response instead of creating a second task.

Long work is not run inline. An executable plan is persisted and queued through
the existing job table, and the caller receives `202` immediately. A high-risk
plan is stored as `waiting_for_approval` and is not queued. Voice invocation
does not change that waiting state.

When no model provider is configured, a conversational request tagged
`siri` or `google_assistant` receives a handoff response naming that source.
The response status is `handed_off`, `handoff` is true, and `device_assistant`
names the source. The backend does not invoke a native assistant or invent an
answer. An executable keyword plan still queues. `web`, `desktop`, and `api`
have no on-device assistant, so those conversational requests stay on the
existing unconfigured path. Conversation messages accept the same optional
`source` and follow the same rule.

## Devices and sync

`POST /api/v1/devices` registers a client (`ios`, `android`, `web`, `desktop`,
`api`). `DELETE /api/v1/devices/{id}` revokes it, after which commands naming
that device are rejected with `403`. A device id is attribution only; the
request is authenticated by the user's token, and a device owned by another
account returns `404`.

`GET /api/v1/sync?after={cursor}` replays durable events newer than the cursor,
scoped to the authenticated user. Reconnecting after a gap recovers the missed
events. Asking twice with the same cursor returns the same rows.

## What is not implemented, and why

Neither platform offers a zero-install way for a voice assistant to reach an
authenticated backend.

- **Siri.** App Intents and SiriKit run inside an app the user installs. Apple
  provides no server API that lets a backend receive a Siri request, and no
  mechanism for Siri to call an arbitrary HTTPS endpoint with the user's
  credentials. A native iOS app is required for "Hey Siri, ask JARVIS…". It is
  an optional future enhancement and is not built here, because the project
  installs nothing on a personal device without explicit approval.
- **Google Assistant.** App Actions are declared in an Android app's
  `shortcuts.xml` and fulfilled by that app. There is no supported way for
  Assistant to make an authenticated request to an external backend on its own.
  A native Android app is the same kind of future enhancement, and is not built.

Until those apps exist, the contract is reachable from the web client and from
any HTTP client that holds a valid access token. The backend side is tested in
`tests/assistant/test_assistant.py`. No physical device has invoked it, and this document
does not claim otherwise.

## Adding a native app later

A future iOS or Android app would register its intents, receive the system
callback on-device, and call `POST /api/v1/assistant/commands` with the user's
token and its registered `device_id`. No planner, registry, or queue change is
needed, because the contract already separates the interaction source from the
reasoning provider.
