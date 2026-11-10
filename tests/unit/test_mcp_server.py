import base64
import json
from types import SimpleNamespace

import pytest
from agentenv import make_env
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from test_healer_loop import GOOD, ok, stuck

from mendarr.api.mcp_server import _client, build_server, mount_mcp, parse_tokens, token_id
from mendarr.config import Settings
from mendarr.rpc import SolveResult, TraceData

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
TOOLS = {
    "list_incidents", "get_incident", "get_trace", "get_plugin", "get_sdk_reference", "test_plugin", "submit_plugin"
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    yield e
    e.close()


def text(res) -> str:
    return res.content[0].text


async def test_tools_and_reads(env):
    await env.fail()
    async with create_connected_server_and_client_session(build_server(env.gate)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOLS
        assert "example.com" in text(await c.call_tool("list_incidents", {}))
        assert "async def solve" in text(await c.call_tool("get_sdk_reference", {}))
        assert json.loads(text(await c.call_tool("get_plugin", {"scope": "_default"})))["source"]


async def test_get_trace_image_and_unknown(env):
    tid = env.traces.save(
        domain="example.com",
        url="http://example.com/x",
        kind="failure",
        result=SolveResult(trace=TraceData(screenshot_b64=PNG, dom="<html><iframe></iframe></html>")),
    )
    async with create_connected_server_and_client_session(build_server(env.gate)) as c:
        res = await c.call_tool("get_trace", {"trace_id": tid})
        assert not res.isError
        assert [x.type for x in res.content] == ["text", "text", "image"]
        assert res.content[2].mimeType == "image/png"
        assert (await c.call_tool("get_trace", {"trace_id": "nope--1-abcdef"})).isError


async def test_submit_failing_then_passing(env):
    await env.fail()
    async with create_connected_server_and_client_session(build_server(env.gate)) as c:
        env.worker.solve_results = [stuck()]
        res = await c.call_tool("submit_plugin", {"source": GOOD, "scope": "example.com"})
        assert not res.isError and json.loads(text(res))["result"] == "replay_failed"
        env.worker.solve_results = [ok()]
        res = await c.call_tool("submit_plugin", {"source": GOOD, "scope": "example.com"})
        assert not res.isError and json.loads(text(res))["result"] == "passed"
    [inc] = env.store.incidents()
    assert {a["model"] for a in env.store.attempts(inc["id"]) if a["tier"] == 2} == {"local"}


def test_client_id_from_request():
    req = SimpleNamespace(headers={"authorization": "Bearer abc"})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=req))
    assert _client(ctx) == token_id("abc")
    assert _client(SimpleNamespace(request_context=SimpleNamespace(request=None))) == "local"


def test_http_auth(env, tmp_path):
    app = FastAPI()
    t1, t2 = "t" * 20, "u" * 20
    settings = Settings(data_dir=str(tmp_path), mcp_token=f"{t1},{t2}")
    mount = mount_mcp(
        app, settings, store=env.store, router=env.router, traces=env.traces, config=env.healer._config, healer=None
    )
    assert mount is not None and mount.tokens == 2
    with TestClient(app) as c:
        r = c.post("/mcp", headers=HEADERS, json=BODY)
        assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
        assert c.post("/mcp", headers={**HEADERS, "Authorization": "Bearer wrong"}, json=BODY).status_code == 401
        for tok in (t1, t2):
            r = c.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {tok}"}, json=BODY)
            assert r.status_code == 200 and "submit_plugin" in r.text


def test_mcp_off_without_token(env, tmp_path):
    app = FastAPI()
    mount = mount_mcp(
        app, Settings(data_dir=str(tmp_path), mcp_token=""), store=env.store, router=env.router,
        traces=env.traces, config=env.healer._config, healer=None,
    )
    assert mount is None
    assert TestClient(app).post("/mcp", headers=HEADERS, json=BODY).status_code == 404


def test_token_helpers():
    assert token_id("a") != token_id("b") and token_id("a") == token_id("a")
    assert parse_tokens(" a, ,b ") == ["a", "b"]
