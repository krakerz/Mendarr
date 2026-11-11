"""Dashboard end-to-end checks against a running Mendarr (see the docker-storage test env)."""

import os
import re
from urllib.parse import urlparse

import httpx
import pytest
from e2e_env import API, FIXTURE, v1

pytestmark = pytest.mark.e2e

USER = os.environ.get("MENDARR_E2E_UI_USER", "admin")
PASSWORD = os.environ.get("MENDARR_E2E_UI_PASSWORD", "admin")


def csrf(c: httpx.Client) -> str:
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', c.get("/ui").text).group(1)


@pytest.fixture
def client():
    with httpx.Client(base_url=API, follow_redirects=False, timeout=30) as c:
        yield c


@pytest.fixture
def session(client):
    r = client.post("/ui/login", data={"user": USER, "password": PASSWORD, "next": "/ui"})
    assert r.status_code == 303
    client.csrf = csrf(client)
    return client


def test_login_flow(client):
    r = client.get("/ui")
    assert r.status_code == 303 and "/ui/login" in r.headers["location"]
    r = client.post("/ui/login", data={"user": USER, "password": PASSWORD, "next": "/ui"})
    assert r.status_code == 303 and "mendarr_session" in client.cookies
    assert client.get("/ui").status_code == 200


@pytest.mark.parametrize(
    "path,heading",
    [
        ("/ui", "<h1>"),
        ("/ui/incidents", "<h1>Incidents"),
        ("/ui/plugins", "<h1>Plugins"),
        ("/ui/plugins/_default", "<h1>"),
        ("/ui/cookies", "<h1>Cookies"),
        ("/ui/settings", "<h1>Settings"),
    ],
)
def test_pages(session, path, heading):
    r = session.get(path)
    assert r.status_code == 200 and heading in r.text


def test_static(client):
    assert client.get("/ui/static/vendor/htmx/htmx.min.js").status_code == 200
    assert 'id="siren"' in client.get("/ui/static/vendor/lucide/sprite.svg").text


def test_solve_then_cookies_listed(session):
    code, body = v1({"cmd": "request.get", "url": f"{FIXTURE}/cf/pass"})
    assert code == 200 and body["message"] == "Challenge solved!"
    host = urlparse(FIXTURE).hostname
    assert host in session.get("/ui").text
    assert "cf_clearance" in session.get("/ui/cookies").text
    assert session.post("/ui/cookies/purge", data={"domain": host}).status_code == 403
    r = session.post("/ui/cookies/purge", data={"domain": host, "csrf": session.csrf})
    assert r.status_code == 303


def test_logout_ends_session(session):
    assert session.post("/ui/logout", data={"csrf": session.csrf}).status_code == 303
    assert session.get("/ui").status_code == 303


def test_active_solves_endpoint(session):
    r = session.get("/ui/active")
    assert r.status_code == 200
    assert 'id="active-solves"' in r.text
    page = session.get("/ui").text
    assert 'hx-get="/ui/active"' in page
    assert "In progress" in page
