"""MCP end to end: an agent heals an escalated incident through /mcp."""

import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from e2e_env import API, v1
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_healer_e2e import BROKEN, FAKELLM, active_plugin, attempts, close_open, seed_broken, trigger, wait_state

pytestmark = pytest.mark.e2e

TOKEN = os.environ.get("MENDARR_E2E_MCP_TOKEN", "mendarr-e2e-mcp-token-0123456789")
HOST = "fixture-mcp"
URL = f"http://{HOST}:8080/cf/interactive"
TOOLS = {
    "list_incidents",
    "get_incident",
    "get_trace",
    "get_plugin",
    "get_sdk_reference",
    "test_plugin",
    "submit_plugin",
}


@asynccontextmanager
async def mcp_session():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=240)) as http:
        async with streamable_http_client(f"{API}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=240)) as s:
                await s.initialize()
                yield s


def texts(result) -> list[str]:
    return [c.text for c in result.content if c.type == "text"]


def images(result) -> list:
    return [c for c in result.content if c.type == "image"]


def first_json(result) -> dict:
    return json.loads(texts(result)[0])


def test_mcp_needs_token():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    assert httpx.post(f"{API}/mcp", json=body, headers=headers, timeout=10).status_code == 401
    wrong = {**headers, "Authorization": "Bearer wrong"}
    assert httpx.post(f"{API}/mcp", json=body, headers=wrong, timeout=10).status_code == 401


async def test_agent_heals_escalated_incident():
    httpx.post(f"{FAKELLM}/reset", json={"script": ["guard"]}, timeout=10).raise_for_status()
    close_open(HOST)
    seed_broken(HOST)
    trigger(HOST)
    inc = wait_state(HOST, {"escalated"}, 120)

    hooks = httpx.get(f"{FAKELLM}/state", timeout=10).json()["hooks"]
    assert any(
        h["path"] == "/hook/generic"
        and h.get("json", {}).get("event") == "agent_wanted"
        and h["json"].get("domain") == HOST
        for h in hooks
    )

    async with mcp_session() as s:
        assert TOOLS <= {t.name for t in (await s.list_tools()).tools}

        listed = await s.call_tool("list_incidents", {})
        rows = json.loads(texts(listed)[0])
        rows = rows if isinstance(rows, list) else rows.get("incidents", rows.get("result", []))
        assert any(r["id"] == inc["id"] and r["state"] == "escalated" for r in rows)

        trace = await s.call_tool("get_trace", {"trace_id": inc["trace_id"]})
        assert any(i.mimeType == "image/png" for i in images(trace))
        # the widget iframe sits in a closed shadow root, so it shows up as a frame entry, not an <iframe> tag
        assert any("fake/turnstile" in t for t in texts(trace))

        plugin = await s.call_tool("get_plugin", {"scope": HOST})
        assert "fault injection" in "".join(texts(plugin))
        sdk = await s.call_tool("get_sdk_reference", {})
        assert "def solve" in "".join(texts(sdk))

        guard = await s.call_tool("test_plugin", {"source": "import os\n" + BROKEN, "url": URL})
        assert first_json(guard)["result"] == "guard"
        failed = first_json(await s.call_tool("test_plugin", {"source": BROKEN, "url": URL}))
        assert failed["ok"] is False and failed["result"] == "replay_failed" and failed["trace_id"]
        failed_full = await s.call_tool("test_plugin", {"source": BROKEN, "url": URL})
        assert images(failed_full)

        refused = await s.call_tool("submit_plugin", {"source": BROKEN, "scope": "fixture-nothing-open"})
        assert refused.isError

        default = await s.call_tool("get_plugin", {"scope": "_default"})
        good = "# mcp e2e\n" + first_json(default)["source"]
        passed = first_json(await s.call_tool("submit_plugin", {"source": good, "scope": HOST, "notes": "mcp e2e"}))
        assert passed["ok"] is True and passed["result"] == "passed"

    solved = wait_state(HOST, {"solved"}, 30)
    assert any(a["tier"] == 2 and a["result"] == "passed" for a in attempts(solved))
    assert active_plugin(HOST)["author"] == "agent"
    hooks = httpx.get(f"{FAKELLM}/state", timeout=10).json()["hooks"]
    assert any(
        h["path"] == "/hook/generic"
        and h.get("json", {}).get("event") == "healed"
        and h["json"].get("domain") == HOST
        for h in hooks
    )
    status, body = v1({"cmd": "request.get", "url": URL, "maxTimeout": 40000})
    assert status == 200 and body["status"] == "ok"
