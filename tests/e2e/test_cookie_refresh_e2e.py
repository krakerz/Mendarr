"""Cookie cache lifetime + background refresher e2e (fixture site only, never a real one).

Opt-in: needs the stack started with the short-interval override (docker-storage/mendarr/compose.refresh.yml:
MENDARR_COOKIE_MAX_TTL_HOURS=auto, MENDARR_COOKIE_REFRESH_INTERVAL=20s, debug logs, egress TTL 10 s) and
MENDARR_E2E_REFRESH=1. The first refresher round runs 20 s after the API starts, then every 20 s.
"""

import os
import re
import subprocess
import time
from urllib.parse import urlparse

import httpx
import pytest
from e2e_env import API, COMPOSE, FIXTURE, FIXTURE_HOST, v1

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("MENDARR_E2E_REFRESH") != "1", reason="needs compose.refresh.yml; set MENDARR_E2E_REFRESH=1"),
]

USER = os.environ.get("MENDARR_E2E_UI_USER", "admin")
PASSWORD = os.environ.get("MENDARR_E2E_UI_PASSWORD", "admin")
HOST = urlparse(FIXTURE).hostname
ROUND_S = 20


@pytest.fixture
def ui():
    with httpx.Client(base_url=API, follow_redirects=False, timeout=30) as c:
        r = c.post("/ui/login", data={"user": USER, "password": PASSWORD, "next": "/ui"})
        assert r.status_code == 303
        c.csrf = re.search(r'name="csrf-token" content="([0-9a-f]+)"', c.get("/ui").text).group(1)
        purge(c)
        yield c
        purge(c)


def purge(c: httpx.Client) -> None:
    assert c.post("/ui/cookies/purge", data={"domain": HOST, "csrf": c.csrf}).status_code == 303


def get(path: str) -> tuple[int, dict]:
    code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}{path}"})
    print(f"{path} -> {code} {body.get('message')!r}")
    return code, body


def names(body: dict) -> set[str]:
    return {c["name"] for c in body["solution"]["cookies"]}


def cookies_page(ui: httpx.Client) -> str:
    return ui.get("/ui/cookies").text


def wait_for(predicate, timeout: float, what: str, every: float = 2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = predicate()
        if got:
            return got
        time.sleep(every)
    pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")


def api_logs() -> str:
    r = subprocess.run(
        ["docker", "compose", "-f", COMPOSE, "logs", "--no-color", "mendarr"],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout + r.stderr


def test_auto_ttl_follows_clearance(ui):
    """With cache lifetime `auto` the entry lives as long as cf_clearance, and the Cookies page says so."""
    code, body = get("/cf/pass?ttl=120")
    assert code == 200 and body["message"] == "Challenge solved!"
    page = cookies_page(ui)
    assert "cf_clearance" in page and "sets lifetime" in page
    # a second request replays the cache
    code, body = get("/cf/pass?ttl=120")
    assert code == 200 and "cf_clearance" in names(body)


def test_session_clearance_uses_fallback(ui):
    """cf_clearance without an expiry: the entry is cached anyway (24 h fallback) and shown as a session cookie."""
    code, body = get("/cf/pass?session=1")
    assert code == 200 and body["message"] == "Challenge solved!"
    page = cookies_page(ui)
    assert "cf_clearance" in page and "session" in page
    code, body = get("/cf/pass?session=1")
    assert code == 200 and "cf_clearance" in names(body)


def test_short_cookie_drops_out_of_cache(ui):
    """A cookie with its own short Max-Age leaves the cache when it expires; the long-lived ones stay."""
    code, body = get("/cf/pass?ttl=600&extra=15")
    assert code == 200 and {"cf_clearance", "fixture_short"} <= names(body)
    time.sleep(ROUND_S)
    code, body = get("/cf/pass?ttl=600&extra=15")
    assert code == 200
    got = names(body)
    assert "cf_clearance" in got and "fixture_short" not in got


def test_refresher_checks_then_resolves_after_reset(ui):
    """Clean round -> ok; fixture forgets the clearance -> next round re-solves; then requests are cache hits again."""
    code, _ = get("/cf/pass?ttl=3600")
    assert code == 200
    wait_for(lambda: re.search(r">\s*ok\s*<", cookies_page(ui)), 3 * ROUND_S + 10, "refresher status ok")
    assert httpx.post(f"{FIXTURE_HOST}/reset", timeout=10).status_code == 204
    wait_for(lambda: re.search(r">\s*solved\s*<", cookies_page(ui)), 3 * ROUND_S + 10, "refresher status solved")
    code, body = get("/cf/pass?ttl=3600")
    assert code == 200 and "cf_clearance" in names(body)
    logs = api_logs()
    assert re.search(r"request: domain=\S+ url=/cf/pass via=cache_hit", logs)
    assert "cookie refresh: checked=" in logs


def test_refresher_renews_expiring_entry(ui):
    """An entry about to expire is renewed in the background instead of lapsing."""
    code, _ = get("/cf/pass?ttl=30")
    assert code == 200
    wait_for(lambda: re.search(r">\s*renewed\s*<", cookies_page(ui)), 4 * ROUND_S + 10, "refresher status renewed")


def test_request_log_has_no_secrets(ui):
    """One INFO line per request: domain, path only, outcome; never the query string or a cookie value."""
    code, body = get("/cf/pass?token=SECRET123")
    assert code == 200
    values = [c["value"] for c in body["solution"]["cookies"] if c["name"] == "cf_clearance"]
    assert values
    time.sleep(1)
    logs = api_logs()
    assert re.search(r"request: domain=\S+ url=/cf/pass via=(solved|cache_hit|no_challenge)", logs)
    assert "SECRET123" not in logs
    for v in values:
        assert v not in logs
    assert not re.search(r"DEBUG.*(websockets|httpcore)", logs)
