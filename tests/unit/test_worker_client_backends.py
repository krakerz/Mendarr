import json

import httpx

from mendarr.api.worker_client import HttpWorkerClient

_STATUS = {"name": "obscura", "component": "binary", "installed": "v1", "state": "idle"}


async def test_backend_calls_hit_the_worker_endpoints():
    seen: list[tuple[str, str, bytes]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append((r.method, r.url.path, r.content))
        if r.url.path in ("/backends", "/backends/check", "/backends/restart"):
            return httpx.Response(200, json={"auto_update": True, "backends": [_STATUS], "closed": 2})
        return httpx.Response(200, json=_STATUS)

    client = HttpWorkerClient("http://worker")
    client._http = httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(handler))
    assert (await client.backends()).backends[0].name == "obscura"
    assert (await client.backends_check()).auto_update is True
    assert (await client.backend_update("obscura")).installed == "v1"
    assert (await client.backend_revert("camoufox")).state == "idle"
    assert (await client.backends_restart(force=True)).closed == 2
    assert [(m, p) for m, p, _ in seen] == [
        ("GET", "/backends"),
        ("POST", "/backends/check"),
        ("POST", "/backends/obscura/update"),
        ("POST", "/backends/camoufox/revert"),
        ("POST", "/backends/restart"),
    ]
    assert json.loads(seen[-1][2]) == {"force": True}
