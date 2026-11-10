"""/ui/vnc/{slot}: authenticated WebSocket proxy to the worker's VNC bridge (one exposed port, dashboard auth)."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import websockets.asyncio.client
import websockets.exceptions
from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect

from mendarr.api.auth import require_user

log = logging.getLogger(__name__)

router = APIRouter()

Connect = Callable[..., Any]  # websockets.asyncio.client.connect, injectable for tests


def upstream_url(worker_url: str, slot: int) -> str:
    u = urlsplit(worker_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}{u.path.rstrip('/')}/vnc/{slot}"


async def _client_to_up(client: WebSocket, up: Any) -> None:
    while True:
        msg = await client.receive()
        if msg["type"] == "websocket.disconnect":
            return
        await up.send(msg.get("bytes") or (msg.get("text") or "").encode())


async def _up_to_client(client: WebSocket, up: Any) -> None:
    async for data in up:
        await client.send_bytes(data if isinstance(data, bytes) else data.encode())


async def _pump(client: WebSocket, up: Any) -> None:
    tasks = [asyncio.ensure_future(_client_to_up(client, up)), asyncio.ensure_future(_up_to_client(client, up))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await up.close()
        with contextlib.suppress(Exception):
            await client.close()


# auth + Origin==Host are enforced by require_user (closes 1008 before this body runs)
@router.websocket("/ui/vnc/{slot}", dependencies=[Depends(require_user)])
async def vnc(websocket: WebSocket, slot: int) -> None:
    app = websocket.app
    settings = app.state.settings
    if app.state.manual.active_for_slot(slot) is None:
        await websocket.close(code=4404)
        return
    headers = {"Authorization": f"Bearer {settings.worker_token}"} if settings.worker_token else {}
    connect = getattr(app.state, "vnc_connect", None) or websockets.asyncio.client.connect
    sub = "binary" if "binary" in websocket.scope.get("subprotocols", []) else None
    try:
        async with connect(
            upstream_url(settings.worker_url, slot),
            additional_headers=headers,
            subprotocols=["binary"],
            max_size=None,
            open_timeout=5,
            ping_interval=None,
        ) as up:
            await websocket.accept(subprotocol=sub)
            await _pump(websocket, up)
    except (OSError, TimeoutError, websockets.exceptions.WebSocketException, WebSocketDisconnect) as e:
        log.warning("vnc proxy slot %s: %s", slot, e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
