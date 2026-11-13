import logging

import httpx

from mendarr.api.worker_client import HttpWorkerClient


async def test_call_logs_method_path_status_at_debug(caplog):
    client = HttpWorkerClient("http://worker")
    client._http = httpx.AsyncClient(
        base_url="http://worker", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}))
    )
    with caplog.at_level(logging.DEBUG, logger="mendarr.api.worker_client"):
        await client._call("GET", "/egress")
    assert "worker GET /egress -> 200" in caplog.text
