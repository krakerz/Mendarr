"""MCP endpoint (/mcp, streamable HTTP, bearer token): external agents heal through the healer's gate."""

import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mendarr.config import Settings
from mendarr.core.router import Router
from mendarr.core.traces import TraceStore
from mendarr.healer import prompt
from mendarr.healer.agent import AgentError, AgentGate, ReplayReport
from mendarr.healer.config import ConfigFile
from mendarr.healer.loop import Healer
from mendarr.plugins.store import PluginStore

log = logging.getLogger(__name__)

PATH = "/mcp"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
INSTRUCTIONS = """\
Mendarr solves Cloudflare challenges with small per-domain Python plugins. When a plugin stops working an
incident opens. Typical repair: list_incidents -> get_incident -> get_trace (screenshot + DOM) -> get_plugin ->
get_sdk_reference -> test_plugin on the incident URL until it passes -> submit_plugin. A submit is replayed on
the incident URL and promoted only when Mendarr's own success judge passes; nothing bypasses that gate.
"""
SUBMIT_NOTES = """

## Submitting
- test_plugin(source, url) replays a plugin once on any http(s) URL and changes nothing.
- submit_plugin(source, scope) stores it for the domain (author "agent"), replays it on the domain's open
  incident URL and promotes it only if the replay passes; a failed replay marks the version rejected.
- Both count against a per-token hourly replay limit; guard rejections don't.
"""


def parse_tokens(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def token_id(token: str) -> str:
    return "agent-" + sha256(token.encode()).hexdigest()[:8]


class BearerGate:
    """Every request needs one of the bearer tokens; compared in constant time against all of them."""

    def __init__(self, app: ASGIApp, tokens: list[str]) -> None:
        self._app = app
        self._tokens = [t.encode() for t in tokens]

    def _ok(self, header: str) -> bool:
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return False
        g = given.strip().encode()
        hit = False
        for t in self._tokens:
            hit |= hmac.compare_digest(g, t)
        return hit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._ok(Headers(scope=scope).get("authorization", "")):
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "missing or wrong bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mendarr"'},
            )
            await resp(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _client(ctx: Context) -> str:
    req = ctx.request_context.request
    if req is None:
        return "local"
    _, _, given = req.headers.get("authorization", "").partition(" ")
    return token_id(given.strip()) if given.strip() else "local"


def _dump(obj: object) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _with_shot(gate: AgentGate, report: ReplayReport) -> list:
    out: list = [_dump(report.as_dict())]
    if report.trace_id:
        try:
            shot = gate.trace(report.trace_id).screenshot
        except AgentError:
            shot = None
        if shot:
            out.append(Image(data=shot, format="png"))
    return out


def build_server(gate: AgentGate) -> FastMCP:
    mcp = FastMCP(
        name="mendarr",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path=PATH,
        # the bearer token is the protection; FastMCP's default localhost-only Host check would refuse LAN clients
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def list_incidents(include_closed: bool = False) -> str:
        """Healing incidents, newest first: open ones (open, healing, escalated = waiting for an agent, manual),
        or the last 50 of any state. Each has the failing url, failure kind and the trace_id of its failure."""
        return _dump(gate.incidents(include_closed))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_incident(incident_id: int) -> str:
        """One incident with its active plugin and attempt log (what retries, the local LLM and agents already
        tried, with replay trace ids). Read it before writing a plugin so you don't repeat a failed idea."""
        return _dump(gate.incident(incident_id))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_trace(trace_id: str, dom_chars: int = 40000) -> list:
        """A failure or replay trace: metadata (final url, title, cf headers, cookies, frames, console, network,
        plugin log steps), the trimmed DOM (scripts/styles removed, shadow roots flattened, child frames listed)
        and the screenshot as an image."""
        t = gate.trace(trace_id)
        meta = dict(t.meta)
        meta["network"] = (meta.get("network") or [])[:60]
        meta["console"] = (meta.get("console") or [])[:40]
        meta["steps"] = (meta.get("steps") or [])[-80:]
        n = max(1000, min(dom_chars, 200_000))
        dom = t.dom[:n] + ("\n<!-- truncated -->" if len(t.dom) > n else "")
        out: list = [f"## Trace {t.id}\n" + _dump(meta), "## DOM\n" + dom]
        if t.screenshot:
            out.append(Image(data=t.screenshot, format="png"))
        return out

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_plugin(scope: str, version: int | None = None) -> str:
        """The plugin a domain runs (its active version, else the _default one), or one exact version, with
        source and the scope's version list. scope = registrable domain like example.org, or _default."""
        return _dump(gate.plugin(scope, version))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_sdk_reference() -> str:
        """Plugin rules and the SDK/Playwright reference every plugin must stick to (the guard enforces it)."""
        return prompt.SYSTEM + "\n\n" + prompt.sdk_reference() + SUBMIT_NOTES

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True), structured_output=False)
    async def test_plugin(source: str, url: str, ctx: Context) -> list:
        """Replay a complete plugin file once on an http(s) URL in a fresh browser slot. Stores nothing. Returns
        the verdict (ok, failure kind, final url/title, cookies, the plugin's log steps) and, on failure, a trace id
        and the screenshot. Counts against the hourly replay limit."""
        return _with_shot(gate, await gate.test(source, url, _client(ctx)))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True), structured_output=False)
    async def submit_plugin(source: str, scope: str, ctx: Context, notes: str | None = None) -> list:
        """Submit a complete plugin file for a domain with an open incident. It is guard-checked, replayed on the
        incident URL and promoted only if Mendarr's judge passes (then the incident is solved); otherwise it is
        rejected and the replay trace returned. Counts against the hourly replay limit."""
        return _with_shot(gate, await gate.submit(source, scope, _client(ctx), notes))

    return mcp


@dataclass(frozen=True)
class McpMount:
    server: FastMCP
    gate: AgentGate
    tokens: int


def mount_mcp(
    app: FastAPI,
    settings: Settings,
    *,
    store: PluginStore,
    router: Router,
    traces: TraceStore,
    config: ConfigFile,
    healer: Healer | None,
) -> McpMount | None:
    tokens = parse_tokens(settings.mcp_token)
    if not tokens:
        log.info("MCP is off (MENDARR_MCP_TOKEN not set)")
        return None
    if any(len(t) < 16 for t in tokens):
        log.warning("an MCP token is shorter than 16 characters; use long random tokens")
    gate = AgentGate(store, router, traces, config, healer, replays_per_hour=settings.mcp_replays_per_hour)
    server = build_server(gate)
    server.streamable_http_app()  # creates server.session_manager
    app.router.routes.append(Route(PATH, endpoint=BearerGate(StreamableHTTPASGIApp(server.session_manager), tokens)))
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state, server.session_manager.run():
            yield state

    app.router.lifespan_context = lifespan
    log.info("MCP enabled at %s (%d token(s))", PATH, len(tokens))
    return McpMount(server, gate, len(tokens))
