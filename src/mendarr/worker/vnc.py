"""WebSocket <-> x11vnc TCP bridge (what websockify did), only while a manual session holds the slot."""

import asyncio
import contextlib
import logging
import time

from fastapi import WebSocket

log = logging.getLogger("mendarr.worker.vnc")

FIRST_FRAME_BYTES = 256 * 1024  # roughly when a first framebuffer update has gone out


async def bridge(
    ws: WebSocket,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ended: asyncio.Event,
    label: str = "",
) -> None:
    t0 = time.monotonic()
    sent = 0

    def mark(what: str) -> None:
        log.info("vnc %s: %s after %.2fs", label, what, time.monotonic() - t0)

    async def ws_to_tcp() -> None:
        first = True
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if first:
                mark("first client message")
                first = False
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def tcp_to_ws() -> None:
        nonlocal sent
        while data := await reader.read(65536):
            if not sent:
                mark("first x11vnc bytes")
            before, sent = sent, sent + len(data)
            if before < FIRST_FRAME_BYTES <= sent:
                mark(f"{sent // 1024} KiB sent")
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws()), asyncio.create_task(ended.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        mark(f"closed, {sent // 1024} KiB sent")
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await ws.close()
