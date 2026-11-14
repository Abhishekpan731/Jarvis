"""Worker health. No user data and no database error text in the body."""

from __future__ import annotations

import json

from app.config import settings
from app.db import session_scope
from sqlalchemy import text


async def start_health(runner):
    import asyncio

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            database = "error"
            try:
                async with session_scope() as session:
                    await session.execute(text("SELECT 1"))
                database = "ok"
            except Exception:
                database = "error"
            body = json.dumps(
                {
                    "process": "ok",
                    "database": database,
                    "ready_to_claim": database == "ok" and not runner.shutdown.is_set(),
                }
            ).encode()
            head = (
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head + body)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", settings.worker_health_port)
    return server
