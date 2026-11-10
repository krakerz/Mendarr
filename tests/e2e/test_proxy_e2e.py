"""Upstream proxy: in the test env the worker reaches the fixture only through the upstream-proxy container."""

import json
import os

import httpx
import pytest
from e2e_env import API, FIXTURE, cli, v1

pytestmark = pytest.mark.e2e

PROXY_IP = os.environ.get("MENDARR_E2E_PROXY_IP", "172.31.250.10")
DEFAULT_PROXY = os.environ.get("MENDARR_E2E_DEFAULT_PROXY", "socks5://upstream-proxy:1080")
HTTP_PROXY = os.environ.get("MENDARR_E2E_HTTP_PROXY", "http://upstream-proxy:3128")


def _incidents(host: str) -> list[int]:
    proc = cli("incidents", "list", "--json")
    assert proc.returncode == 0, proc.stderr
    return [i["id"] for i in json.loads(proc.stdout) if i["domain"] == host]


def test_egress_is_proxy_exit():
    body = httpx.get(f"{API}/v1/egress", timeout=30).json()
    assert body["ip"] == PROXY_IP
    assert body["proxy"] == DEFAULT_PROXY


def test_solve_leaves_through_default_proxy():
    code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}/ip"})
    assert code == 200, body
    assert PROXY_IP in body["solution"]["response"]


def test_request_proxy_with_credentials():
    proxy = {"url": HTTP_PROXY, "username": "e2e", "password": "e2e-pass"}
    code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}/ip", "proxy": proxy})
    assert code == 200, body
    assert PROXY_IP in body["solution"]["response"]


def test_dead_request_proxy_fails_without_incident():
    # also proves a request proxy overrides the (working) default proxy
    host = "fixture-proxy"
    before = _incidents(host)
    _, body = v1(
        {
            "cmd": "request.get",
            "url": f"http://{host}:8080/plain",
            "proxy": {"url": "http://upstream-proxy:1"},
            "maxTimeout": 20000,
        }
    )
    assert body["status"] == "error", body
    assert "Upstream proxy http://upstream-proxy:1" in body["message"]
    assert _incidents(host) == before
