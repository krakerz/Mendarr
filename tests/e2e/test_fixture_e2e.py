"""End-to-end tests against the fixture site through a running Mendarr (see docker-storage test env)."""

import html
import time

import httpx
import pytest
from e2e_env import API, FIXTURE, FIXTURE_HOST, ONLINE, v1

pytestmark = pytest.mark.e2e


def get(path: str, **extra) -> tuple[int, dict, float]:
    t = time.monotonic()
    code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}{path}", **extra})
    elapsed = time.monotonic() - t
    print(f"{path} -> {code} {body.get('message')!r} in {elapsed:.1f}s")
    return code, body, elapsed


def cookie_names(body: dict) -> set[str]:
    return {c["name"] for c in body["solution"]["cookies"]}


def test_plain():
    code, body, _ = get("/plain")
    assert code == 200
    assert body["message"] == "Challenge not detected!"
    assert "<title>Plain page" in body["solution"]["response"]
    assert "fixture_visit" in cookie_names(body)


def test_error500():
    code, body, _ = get("/error500")
    assert code == 200 and body["status"] == "ok"
    assert body["solution"]["status"] == 500


def test_slow_timeout():
    code, body, elapsed = get("/slow?s=30", maxTimeout=5000)
    assert code == 500
    assert body["message"] == "Error: Error solving the challenge. Timeout after 5.0 seconds."
    assert elapsed < 15


def test_pass():
    code, body, _ = get("/cf/pass")
    assert code == 200
    assert body["message"] == "Challenge solved!"
    assert "cf_clearance" in cookie_names(body)
    assert "Fixture OK" in body["solution"]["response"]


def test_interactive():
    code, body, _ = get("/cf/interactive")
    assert code == 200
    assert body["message"] == "Challenge solved!"


def test_block():
    code, body, _ = get("/cf/block", maxTimeout=30000)
    assert code == 500
    assert body["mendarrFailure"] == "challenge_loop"
    assert body["message"].endswith("Timeout after 30.0 seconds.")


def test_loop():
    code, body, _ = get("/cf/loop", maxTimeout=30000)
    assert code == 500
    assert body["mendarrFailure"] == "challenge_loop"


def test_denied():
    code, body, _ = get("/cf/denied")
    assert code == 500
    assert body["message"].startswith("Error:")
    assert "blocked" in body["message"].lower()


def test_cookie_cache():
    _, first, _ = get("/cf/pass")
    assert first["message"] == "Challenge solved!"
    _, second, elapsed = get("/cf/pass")
    assert second["message"] == "Challenge not detected!"
    assert second["solution"]["userAgent"] == first["solution"]["userAgent"]
    assert elapsed < 5
    httpx.post(f"{FIXTURE_HOST}/reset", timeout=10)
    _, third, _ = get("/cf/pass")
    assert third["message"] == "Challenge solved!"  # stale cache entry dropped


def test_post_echo():
    code, body = v1({"cmd": "request.post", "url": f"{FIXTURE}/echo", "postData": "a=1&b=2"})
    assert code == 200
    page = html.unescape(body["solution"]["response"])
    assert '"a"' in page and '"1"' in page
    assert "POST" in page


def test_sessions():
    assert v1({"cmd": "sessions.create", "session": "e2e-s1"})[0] == 200
    try:
        code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}/plain", "session": "e2e-s1"})
        assert code == 200
        assert "e2e-s1" in v1({"cmd": "sessions.list"})[1]["sessions"]
    finally:
        assert v1({"cmd": "sessions.destroy", "session": "e2e-s1"})[0] == 200
    assert "e2e-s1" not in v1({"cmd": "sessions.list"})[1]["sessions"]


def test_egress():
    r = httpx.get(f"{API}/v1/egress", timeout=30)
    assert r.status_code == 200
    ip = r.json().get("ip")
    assert ip and ip != "unknown"


@pytest.mark.e2e_online
@pytest.mark.skipif(not ONLINE, reason="MENDARR_E2E_ONLINE=1 not set")
def test_real_widget_pass():
    _, body, _ = get("/cf/pass?widget=real")
    assert body["message"] == "Challenge solved!"


@pytest.mark.e2e_online
@pytest.mark.skipif(not ONLINE, reason="MENDARR_E2E_ONLINE=1 not set")
def test_real_widget_interactive():
    _, body, _ = get("/cf/interactive?widget=real")
    assert body["message"] == "Challenge solved!"
