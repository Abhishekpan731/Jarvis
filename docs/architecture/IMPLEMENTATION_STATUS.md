# JARVIS — Implementation Status

Audited 2026-11-15 against the working tree, not against earlier notes. A row
is verified only when a command was run and its output observed.

| Feature | Existing implementation | Verified | Remaining work |
|---|---|---|---|
| Authentication | Register, login, logout, refresh with rotation and family revocation, session lookup, password change, soft account deletion. Argon2id, HttpOnly refresh cookie, in-process login throttle. Password change and deletion revoke every session for the account. | Yes. `tests/test_auth.py` and `tests/test_review_regressions.py`, against PostgreSQL. | Redis-backed rate limiting. Hard deletion of user data. |
| Database | PostgreSQL 16 with pgvector, migrations `0001` and `0002`. | Yes. `alembic upgrade head` then `alembic current` reports `0002 (head)`. 18 agents, 51 capabilities, extensions `pgcrypto`, `plpgsql`, `vector`. | Row-level security is enabled but not forced; the application role owns the tables, so it bypasses it. |
| Agents | 18 agents in `jarvis_shared.agents` and seeded by migration `0001`. | Yes. `select count(*) from agents` returns 18. Registry import returns 18. | No idempotent re-seed command. The migration inserts once; a second run is a no-op because Alembic tracks it. |
| Task execution | In-process executor. Steps of a task, and tasks themselves, run concurrently with `asyncio.gather`. A step that raises, or a plan with no steps, moves the task to `failed` instead of `completed`. | Yes. `tests/test_parallel_execution.py` and `tests/test_review_regressions.py`. | Not durable. A process crash loses the run. No worker, no queue. |
| Frontend | Next.js app in `apps/web`: sign in, register, conversation list, streaming chat, stop control. | Partially. `tsc --noEmit` passes. No browser test was run. | Rename and delete exist in the API client but not in the UI. No microphone. |
| AI integration | Provider interface, OpenAI implementation, factory that reports unconfigured instead of inventing a reply. | Partially. Missing-key path verified: health reports `configured: false` and messaging returns 503 `ai_not_configured`. | No live call was made. No `OPENAI_API_KEY` is set. |
| Voice | Not implemented. | No. | Phase 7. |
| Supervisor | Plans a request, validates agents against the registry, forces approval on high-risk actions. | Yes. `tests/test_supervisor.py`, 4 tests. | Routing uses the model only when one is configured. Without a key it falls back to keyword matching. |
| Memory | Table, CRUD API, keyword search, privacy class, active flag. Updates re-validate the privacy class. | No. Endpoints exist; no test covers them. | No embeddings are generated. The `embedding` column is empty. |
| Streaming | SSE endpoint emitting `response.started`, `text.delta`, `response.completed`, `error`. | No. Code reviewed, not exercised against a provider. | Needs a configured provider to verify end to end. |

## What is explicitly not done

- Durable background execution. The executor runs inside the API process.
- Semantic memory retrieval. The column exists; nothing writes it.
- Wake word, local device control, native applications. Out of scope.
- Live LLM responses. No credential is configured, and none was invented.
