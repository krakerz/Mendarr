from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mendarr import __version__, logsetup
from mendarr.api.auth import Auth
from mendarr.api.flaresolverr import FLARESOLVERR_VERSION
from mendarr.api.mcp_server import mount_mcp
from mendarr.api.ui import router as ui_router
from mendarr.api.ui_manual import router as ui_manual_router
from mendarr.api.v1 import router as v1_router
from mendarr.api.vnc_proxy import router as vnc_router
from mendarr.api.worker_client import HttpWorkerClient, WorkerClient
from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.manual import ManualManager
from mendarr.core.refresher import Refresher
from mendarr.core.router import Router
from mendarr.core.stats import DomainStats, StatsHooks
from mendarr.core.traces import TraceStore
from mendarr.core.tracker import SolveTracker
from mendarr.core.updates import UpdateChecker
from mendarr.healer.config import ConfigFile, config_path
from mendarr.healer.loop import Healer
from mendarr.healer.notify import Notifier
from mendarr.plugins.store import PluginStore, RollbackPolicy
from mendarr.web import STATIC_DIR


def create_app(
    settings: Settings | None = None,
    *,
    worker: WorkerClient | None = None,
    cache: CookieCache | None = None,
    store: PluginStore | None = None,
    update_transport: httpx.AsyncBaseTransport | None = None,  # tests: fake GitHub
) -> FastAPI:
    settings = settings or Settings()
    worker = worker or HttpWorkerClient(settings.worker_url, token=settings.worker_token)
    cache = cache or CookieCache.open(
        Path(settings.data_dir) / "store.db", max_ttl_s=settings.cookie_max_ttl_hours * 3600
    )
    store = store or PluginStore.open(Path(settings.data_dir), policy=RollbackPolicy.from_settings(settings))
    stats = DomainStats.open(Path(settings.data_dir) / "store.db")
    traces = TraceStore.open(Path(settings.data_dir) / "traces")
    config_file = ConfigFile(config_path(settings))
    tracker = SolveTracker()
    router = Router(settings, worker, cache, store=store, tracker=tracker, stats=stats)
    healer = Healer(
        settings, store, router, traces, config_file, notifier=Notifier(public_url=settings.public_url), tracker=tracker
    )
    router.hooks = StatsHooks(stats, healer)
    manual = ManualManager(
        worker,
        store,
        cache,
        traces,
        hold_policy=lambda d: config_file.get().for_domain(d).escalation.manual.hold_requests,
        give_up_s=lambda: config_file.get().healer.give_up_after,
        plugin_resolver=router._resolve_plugin,
    )
    router.manual = manual
    refresher = Refresher(settings, router, cache, worker, config_file, manual)
    update_notifier = Notifier(public_url=settings.public_url)
    updates = UpdateChecker(settings, config_file, worker, update_notifier, transport=update_transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        healer.start()
        refresher.start()
        updates.start()
        try:
            yield
        finally:
            await updates.stop()
            await update_notifier.aclose()
            await refresher.stop()
            await healer.stop()
            await manual.aclose()
            await worker.aclose()
            cache.close()
            stats.close()
            store.close()

    app = FastAPI(title="Mendarr", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.worker = worker
    app.state.store = store
    app.state.router = router
    app.state.manual = manual
    app.state.healer = healer
    app.state.cache = cache
    app.state.traces = traces
    app.state.config_file = config_file
    app.state.stats = stats
    app.state.tracker = tracker
    app.state.refresher = refresher
    app.state.updates = updates
    app.state.auth = Auth.from_settings(settings)
    app.state.user_agent = ""
    app.include_router(v1_router)
    app.include_router(ui_router)
    app.include_router(ui_manual_router)
    app.include_router(vnc_router)
    app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="ui-static")
    app.state.mcp = mount_mcp(
        app, settings, store=store, router=router, traces=traces, config=config_file, healer=healer
    )

    @app.get("/")
    async def index() -> dict[str, str]:
        if not app.state.user_agent:
            app.state.user_agent = await worker.user_agent()
        return {
            "msg": "FlareSolverr is ready!",
            "version": FLARESOLVERR_VERSION,
            "userAgent": app.state.user_agent,
            "mendarrVersion": __version__,
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    s = Settings()
    uv_level = logsetup.configure(s.log_level)
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level=uv_level)
