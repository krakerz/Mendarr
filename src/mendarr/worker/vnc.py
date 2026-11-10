"""WebSocket <-> x11vnc TCP bridge (what websockify did), only while a manual session holds the slot."""

import asyncio
import contextlib

from fastapi import WebSocket


async def bridge(
    ws: WebSocket, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ended: asyncio.Event
) -> None:
    async def ws_to_tcp() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def tcp_to_ws() -> None:
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws()), asyncio.create_task(ended.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await ws.close()
