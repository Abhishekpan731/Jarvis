# JARVIS — Diagrams

Rendered versions of the diagrams in
[ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](../security/SECURITY.md).
Kept separate so the architecture document stays readable.

## Request lifecycle

```mermaid
sequenceDiagram
    actor User
    participant Web as Next.js UI
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Redis
    participant W as Worker

    User->>Web: Instruction (text or voice)
    Web->>API: POST /api/v1/tasks (Bearer token)
    API->>API: Validate token, resolve user
    API->>DB: Insert task (CREATED) + task_event
    API->>Q: Enqueue task id
    API-->>Web: 202 Accepted {task_id}
    W->>Q: Dequeue task id
    W->>DB: Transition CREATED → UNDERSTANDING
    W->>DB: Write plan, transition → EXECUTING
    Note over W,DB: Each step checkpointed in the same transaction
    W->>DB: Transition → COMPLETED + result
    Web->>API: GET /api/v1/tasks/{id}/events (SSE)
    API-->>Web: task.completed
```

Closing the browser drops the SSE connection only. The worker keeps running
because it never depended on the connection.

## Data isolation

```mermaid
flowchart TB
    req["Authenticated request"] --> mw["Auth middleware<br/>resolves user_id from token"]
    mw --> repo["Repository layer<br/>every query filters on user_id"]
    repo --> db[("PostgreSQL<br/>row-level policies")]
    db --> resp["Response scoped to caller"]

    other["Any query without user_id"] -.->|rejected by construction| repo
```

Isolation is enforced twice: the repository layer requires a `user_id`
argument and will not construct a query without one, and PostgreSQL row-level
security rejects rows the session is not permitted to see. Either control
failing closed is sufficient; both are present so one regression is not enough
to leak data.
