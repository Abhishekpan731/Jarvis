# JARVIS — Conversation Engine

## Flow

1. The user posts a message to `/api/v1/conversations/{id}/stream`.
2. The message is stored before any model call, so a provider failure never
   loses what the user typed.
3. `build_context` assembles the system prompt, preferences, up to ten active
   memories, the conversation summary, and recent messages, dropping the oldest
   until the result fits a 6,000-token budget.
4. The provider streams deltas. Each becomes an SSE `text.delta` event.
5. The completed text is stored as an assistant message. If the provider is
   not configured, the stored message says so and names the error code.

A non-streaming variant lives at `POST /conversations/{id}/messages`. It asks
the supervisor to plan first and, when the plan needs execution and no
approval, creates a task through the existing `Task` and `TaskStep` models.

## Provider

`app/ai/provider_factory.py` is the only place that decides which provider
runs. With no `OPENAI_API_KEY` it raises `NotConfigured`, and the API returns
503 `ai_not_configured`. Nothing substitutes a fake answer.

## Supervisor

`app/ai/supervisor.py` returns a `Plan`. Before anything acts on it,
`validate_plan` removes agents that are not in the registry and forces
`requires_approval` when an action contains send, delete, pay, purchase,
deploy, publish, or transfer. The model cannot add an agent or lower a risk.

## Limitations

- Execution is in-process. It does not survive an API restart.
- A streamed reply is stored after the final event. A client that disconnects
  before that point loses the reply. The user's message is already stored.
- Task creation commits separately from the reply. A crash between the two can
  leave a reply without its task.
- Context budgeting counts characters, not tokens. It is a ceiling, not an
  exact bill.
- Cost figures in `app/ai/usage.py` are estimates for two models and are
  labelled as such.
