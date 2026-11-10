import argparse
import asyncio
import functools
import hmac
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from mendarr import __version__
from mendarr.config import Settings
from mendarr.proxy import ProxyConfigError
from mendarr.rpc import (
    CheckRequest,
    CheckResult,
    EgressInfo,
    ManualResult,
    ManualStart,
    ManualState,
    SessionCreate,
    SessionCreated,
    SessionList,
    SolveRequest,
    SolveResult,
    WorkerInfo,
)
from mendarr.worker.backends import BACKENDS, camoufox, patchright
from mendarr.worker.backends.camoufox import Launcher
from mendarr.worker.check import CookieChecker
from mendarr.worker.display import DisplayManager, vnc_port
from mendarr.worker.egress import Egress
from mendarr.worker.executor import Executor
from mendarr.worker.manual import ManualSessions, NoFreeSlot
from mendarr.worker.sessions import SessionManager
from mendarr.worker.slots import SlotPool
from mendarr.worker.vnc import bridge
from mendarr.worker.watchdog import Watchdog

log = logging.getLogger("mendarr.worker.server")

WORKER_PORT = 8192


def create_app(
    settings: Settings | None = None,
    *,
    launcher: Launcher | None = None,
    start_displays: bool = True,
    vnc_port_of: Callable[[int], int] = vnc_port,
) -> FastAPI:
    settings = settings or Settings()
    upstream = settings.upstream  # ProxyConfigError on a bad MENDARR_PROXY_* setting: fail before serving
    if upstream is not None:
        log.info("upstream proxy: all worker traffic goes through %s", upstream)
    displays = DisplayManager(settings.slots)
    egress = Egress(settings.egress_check_url, settings.egress_ttl_seconds, upstream=upstream)
    if launcher is not None:
        launchers = dict.fromkeys(BACKENDS, launcher)
    else:
        launchers = {
            "camoufox": functools.partial(camoufox.launch, settings=settings, exit_ip=egress.exit_ip),
            "patchright": functools.partial(patchright.launch, settings=settings),
        }
    pool = SlotPool(settings.slots, launchers)
    sessions = SessionManager(pool)
    checker = CookieChecker(settings, pool)
    watchdog = Watchdog(settings.plugin_stall_s, settings.plugin_kill_s)
    executor = Executor(settings, pool, sessions, egress, watchdog)
    manual = ManualSessions(pool, egress)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_displays:
            displays.start()
        watchdog.start()
        warm = asyncio.create_task(pool.warm())
        try:
            yield
        finally:
            await watchdog.stop()
            warm.cancel()
            await manual.close_all()
            await sessions.close_all()
            await pool.close()
            if start_displays:
                displays.stop()

    app = FastAPI(title="Mendarr worker", lifespan=lifespan)
    expected = f"Bearer {settings.worker_token}".encode() if settings.worker_token else None
    if expected is not None:

        @app.middleware("http")
        async def require_token(request: Request, call_next):
            # /health stays open for the container healthcheck
            given = request.headers.get("authorization", "").encode()
            if request.url.path != "/health" and not hmac.compare_digest(given, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)
    else:
        log.warning("MENDARR_WORKER_TOKEN is not set: the worker accepts requests from anyone who can reach it")
    app.state.pool = pool
    app.state.sessions = sessions
    app.state.egress = egress
    app.state.checker = checker
    app.state.executor = executor
    app.state.manual = manual

    @app.get("/health")
    def health() -> dict:
        if not start_displays:
            return {"status": "ok", "slots": {}}
        slots = displays.state()
        ok = all(all(s.values()) for s in slots.values())
        return {"status": "ok" if ok else "degraded", "slots": slots}

    @app.get("/info")
    def info() -> WorkerInfo:
        return WorkerInfo(
            user_agent=pool.user_agent(1),
            slots=settings.slots,
            version=__version__,
            cookie_check=checker.mode,
            impersonate=checker.target,
            proxy=str(upstream) if upstream else None,
        )

    @app.post("/solve")
    async def solve(req: SolveRequest) -> SolveResult:
        return await executor.solve(req)

    @app.post("/check")
    async def check(req: CheckRequest) -> CheckResult:
        return await checker.check(req)

    @app.post("/sessions")
    async def create_session(req: SessionCreate) -> SessionCreated:
        session, fresh = await sessions.create(req.session, req.proxy)
        return SessionCreated(session=session.id, fresh=fresh)

    @app.get("/sessions")
    def list_sessions() -> SessionList:
        return SessionList(sessions=sessions.ids())

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        return {"existed": await sessions.destroy(session_id)}

    @app.get("/egress")
    async def get_egress() -> EgressInfo:
        return await egress.info()

    @app.post("/manual")
    async def manual_start(req: ManualStart):
        try:
            return await manual.start(req)
        except NoFreeSlot:
            return JSONResponse({"detail": "no free slot"}, status_code=409)

    @app.get("/manual/{sid}")
    async def manual_state(sid: str) -> ManualState:
        st = await manual.state(sid)
        if st is None:
            raise HTTPException(404, "no such manual session")
        return st

    @app.post("/manual/{sid}/finish")
    async def manual_finish(sid: str) -> ManualResult:
        res = await manual.finish(sid)
        if res is None:
            raise HTTPException(404, "no such manual session")
        return res

    @app.delete("/manual/{sid}")
    async def manual_cancel(sid: str) -> dict:
        return {"existed": await manual.cancel(sid)}

    @app.websocket("/vnc/{slot}")
    async def vnc(ws: WebSocket, slot: int) -> None:
        # the HTTP middleware never sees websockets, so the token is checked here
        given = ws.headers.get("authorization", "").encode()
        if expected is not None and not hmac.compare_digest(given, expected):
            await ws.close(code=4401)
            return
        session = manual.for_slot(slot)
        if session is None:
            await ws.close(code=4404)
            return
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", vnc_port_of(slot))
        except OSError as e:
            log.warning("vnc slot %s: %s", slot, e)
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        await bridge(ws, reader, writer, session.ended)

    return app


def smoke() -> None:
    import subprocess
    import time
    from pathlib import Path

    from camoufox.sync_api import Camoufox

    os.environ.setdefault("DISPLAY", ":1")
    xvfb = None
    # `compose run` starts without the server's displays, so bring up a throwaway one
    if not Path(f"/tmp/.X11-unix/X{os.environ['DISPLAY'].lstrip(':')}").exists():
        os.environ["DISPLAY"] = ":99"
        xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1366x768x24", "-nolisten", "tcp"])
        time.sleep(1)
    try:
        with Camoufox(headless=False) as browser:
            page = browser.new_page()
            page.goto("about:blank")
            print(page.evaluate("navigator.userAgent"))
    finally:
        if xvfb is not None:
            xvfb.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mendarr-worker")
    parser.add_argument("command", nargs="?", choices=["serve", "smoke"], default="serve")
    if parser.parse_args().command == "smoke":
        smoke()
        return
    s = Settings()
    logging.basicConfig(level=s.log_level.upper())
    try:
        app = create_app(s)
    except ProxyConfigError as e:
        raise SystemExit(f"mendarr-worker: {e}") from None
    uvicorn.run(app, host=s.host, port=WORKER_PORT, log_level=s.log_level)
