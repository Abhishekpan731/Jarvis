# JARVIS worker

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

`apps/worker` is the process that executes durable jobs. For runnable tasks,
the API creates a task and inserts a job for the worker to claim. If the worker
is not running, those jobs stay queued.

Nothing in this process is installed on a user device. The browser never talks
to the worker.

## Start

From the repository root, with the API environment already loaded:

```bash
.venv/bin/python -m jarvis_worker
```

The Docker image uses the same entrypoint: `python -m jarvis_worker`. Do not
start `jarvis_worker.loop` as a module. That file defines `main` and does not
run it.

## What one pass does

Each loop:

1. Reaps leases that expired.
2. Promotes jobs whose retry wait has elapsed.
3. Runs due briefings.
4. Claims up to the free concurrency slots with `FOR UPDATE SKIP LOCKED`.
   One account has at most one running job.
5. Heartbeats each claimed lease until the task finishes or the lease is lost.

A lost lease stops the old runner from writing further state for that job; a
second worker can then claim it. A step may be retried while an earlier
external request is still in flight, so execution does not guarantee exactly-
once side effects. Currently, only Google integration steps have real task
handlers, limited to Gmail profile totals and Calendar-list metadata. Other
agent steps return a placeholder `<agent_id>: completed` result rather than
performing their advertised capability.

The worker imports `app.jobs`, `app.execution`, `app.integrations.runner`,
`app.briefing`, `app.config`, and `app.db`. It is a separate process, not a
separate package boundary. The job table and `jarvis_shared.tasks` are the
contract. See [GUIDE.md](../developer/GUIDE.md#appsworker).

## Isolation

Claims run under the `jarvis_app` role with the worker GUC set. Row-level
security still applies. A job for one account cannot be loaded as another
account's task. Logs carry job, task, and worker ids. They do not carry
message text, memory content, or tokens.

## Health

The health check listens on `127.0.0.1:8091`. Override the port with
`WORKER_HEALTH_PORT`. The body reports process status, database reachability,
`ready_to_claim`, queue depth, and in-flight jobs. It does not include user
data or database error text.

For a host-run worker, the endpoint is available only at
`http://127.0.0.1:8091/` (or the configured port). In Compose, the port is not
published to the host; only the container `HEALTHCHECK` can reach it.

## What this process does not do

- It does not accept HTTP commands from the browser.
- It does not bypass approval. A task in `waiting_for_approval` has no job,
  so the worker cannot execute it. The current API has no approval or rejection
  endpoint to move that task forward.
- It does not invent a model reply when `OPENAI_API_KEY` is unset.
