import os
import stat
from urllib.parse import quote

import pytest
from fastapi.routing import _IncludedRouter
from starlette.routing import Mount

from mendarr.api.app import create_app
from mendarr.api.auth import COOKIE, Auth, require_user
from mendarr.config import Settings


def test_no_credentials_refuses_login(client):
    r = client.get("/ui/login")
    assert r.status_code == 200 and "MENDARR_UI_PASSWORD" in r.text
    assert client.post("/ui/login", data={"user": "a", "password": "b"}).status_code == 503
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login?next=%2Fui"


def test_wrong_password(ui_client):
    ui_client.cookies.clear()
    r = ui_client.post("/ui/login", data={"user": "admin", "password": "nope"}, follow_redirects=False)
    assert r.status_code == 401 and "Wrong user or password" in r.text
    assert COOKIE not in r.headers.get("set-cookie", "")


def test_login_cookie_flags_and_page(ui_client):
    ui_client.cookies.clear()
    r = ui_client.post("/ui/login", data={"user": "admin", "password": "admin"}, follow_redirects=False)
    raw = r.headers["set-cookie"]
    assert "HttpOnly" in raw and "samesite=lax" in raw.lower()
    page = ui_client.get("/ui").text
    assert 'href="/ui/manual"' in page and 'name="csrf-token"' in page and "Overview" in page


def test_tampered_json_and_htmx(ui_client):
    tok = ui_client.cookies.get(COOKIE)
    ui_client.cookies.set(COOKIE, tok[:-1] + ("0" if tok[-1] != "0" else "1"))
    r = ui_client.get("/ui", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/login")
    r = ui_client.get("/ui", headers={"Accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401
    r = ui_client.get("/ui", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/ui/login")


def test_auth_unit(tmp_path):
    now = [1000.0]
    s = Settings(data_dir=str(tmp_path), ui_user="u", ui_password="p", ui_secret="s" * 40, ui_session_hours=1)
    auth = Auth(s, b"secret", clock=lambda: now[0])
    tok = auth.issue()
    assert auth.session(tok)[0] == "u"
    other = Auth(s.model_copy(update={"ui_password": "changed"}), b"secret", clock=lambda: now[0])
    assert other.session(tok) is None
    now[0] += 3601
    assert auth.session(tok) is None


def test_csrf(ui_client):
    r = ui_client.post("/ui/cookies/purge", data={"domain": ""}, follow_redirects=False)
    assert r.status_code == 403
    r = ui_client.post(
        "/ui/cookies/purge", data={"domain": ""}, headers={"X-CSRF-Token": ui_client.csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    r = ui_client.post("/ui/cookies/purge", data={"domain": "", "csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303
    r = ui_client.post("/ui/cookies/purge", data={"domain": "", "csrf": "bad"}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example"])
def test_open_redirect(ui_client, target):
    ui_client.cookies.clear()
    r = ui_client.post(
        "/ui/login", data={"user": "admin", "password": "admin", "next": target}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/ui"
    r = ui_client.post(
        "/ui/login", data={"user": "admin", "password": "admin", "next": "/ui/cookies"}, follow_redirects=False
    )
    assert r.headers["location"] == quote("/ui/cookies", safe="/")


def test_logout(ui_client):
    r = ui_client.post("/ui/logout", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    ui_client.cookies.clear()
    assert ui_client.get("/ui", follow_redirects=False).status_code == 303


def test_secret_file(tmp_path, fake_worker):
    s = Settings(data_dir=str(tmp_path), ui_user="u", ui_password="p")
    a = Auth.from_settings(s)
    path = tmp_path / "ui_secret"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert Auth.from_settings(s).session(a.issue()) is not None


def _calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _calls(d)


def _effective_routes(routes):
    # FastAPI >= 0.16x keeps included routers lazy: flatten them into per-route contexts
    for r in routes:
        if isinstance(r, _IncludedRouter):
            yield from _effective_routes(r.effective_candidates())
        else:
            yield r


def test_every_ui_route_requires_user(tmp_path, fake_worker):
    app = create_app(Settings(data_dir=str(tmp_path), healer_enabled=False), worker=fake_worker)
    checked = 0
    for route in _effective_routes(app.routes):
        path = getattr(route, "path", "")
        if not path.startswith("/ui") or path == "/ui/login" or isinstance(route, Mount):
            continue
        dependant = getattr(route, "dependant", None) or route.starlette_route.dependant
        assert require_user in _calls(dependant), path
        checked += 1
    assert checked >= 4
