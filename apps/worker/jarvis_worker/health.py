"""Worker health endpoints. Responses never expose user data or database errors."""

from __future__ import annotations

import json

from app.config import settings
from app.db import worker_session_scope
from app.observe import queue_depth
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


async def start_health(runner):
    """Start the loopback-only HTTP health server for the worker."""
    import asyncio

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_parts = (await reader.readline()).split()
            path = request_parts[1].split(b"?", 1)[0] if len(request_parts) >= 2 else b""
            if path == b"/live":
                status, payload = 200, {"process": "ok"}
            elif path not in (b"/", b"/ready"):
                status, payload = 404, {"error": "not_found"}
            else:
                database = "error"
                depth = None
                try:
                    async with worker_session_scope() as session:
                        await session.execute(text("SELECT 1"))
                        depth = await queue_depth(session)
                    database = "ok"
                except (RuntimeError, SQLAlchemyError):
                    depth = None
                ready = database == "ok" and runner.ready and not runner.shutdown.is_set()
                status = 200 if ready else 503
                payload = {
                    "process": "ok",
                    "database": database,
                    "ready_to_claim": ready,
                    "inflight": runner.inflight,
                    "queue": depth,
                }
            body = json.dumps(payload).encode()
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
            response = (
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(response + body)
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", settings.worker_health_port)
