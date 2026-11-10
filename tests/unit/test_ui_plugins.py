import re

from fastapi.testclient import TestClient


def _post(c: TestClient, path: str, **data):
    return c.post(path, data={"csrf": c.csrf, **data}, follow_redirects=False)


def _src(c: TestClient) -> str:
    return c.app.state.store.get("_default", 1).source


def test_index_lists_default(ui_client):
    r = ui_client.get("/ui/plugins")
    assert r.status_code == 200 and "_default" in r.text


def test_submit_promote_diff_reject_rollback(ui_client):
    c, store = ui_client, ui_client.app.state.store
    src = _src(c)
    r = _post(c, "/ui/plugins/example.org/submit", source=src, notes="first")
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert store.get("example.org", 1).status == "candidate"
    assert _post(c, "/ui/plugins/example.org/promote", version=1).status_code == 303
    assert store.active("example.org").version == 1
    _post(c, "/ui/plugins/example.org/submit", source=src + "\n# changed line\n")
    assert store.get("example.org", 2).version == 2
    page = c.get("/ui/plugins/example.org/diff?a=1&b=2").text
    assert re.search(r'class="add">\+# changed line', page)
    assert c.get("/ui/plugins/example.org").status_code == 200
    assert c.get("/ui/plugins/example.org/v2").status_code == 200
    assert c.get("/ui/plugins/example.org/edit?from=2").status_code == 200
    _post(c, "/ui/plugins/example.org/reject", version=2)
    assert store.get("example.org", 2).status == "rejected"
    r = _post(c, "/ui/plugins/example.org/rollback")
    assert r.status_code == 303 and "ok=" in r.headers["location"]


def test_guard_rejection_keeps_text(ui_client):
    c = ui_client
    bad = "import os\n" + _src(c)
    r = _post(c, "/ui/plugins/example.org/submit", source=bad, notes="n")
    assert r.status_code == 400
    assert "Guard rejected it" in r.text and "import os" in r.text
    assert c.app.state.store.list("example.org") == []


def test_promote_without_csrf_forbidden(ui_client):
    r = ui_client.post("/ui/plugins/example.org/promote", data={"version": 1}, follow_redirects=False)
    assert r.status_code == 403


def test_test_route(ui_client):
    c = ui_client
    src = _src(c)
    r = _post(c, "/ui/plugins/example.org/test", source=src, url="not a url")
    assert r.status_code == 200 and "http(s) URL" in r.text
    r = _post(c, "/ui/plugins/example.org/test", source=src, url="http://example.org/")
    assert r.status_code == 200 and ("passed" in r.text or "failed" in r.text)
    assert "nothing was stored" in r.text.lower()
    assert c.app.state.store.list("example.org") == []


def test_bad_scope_404(ui_client):
    assert ui_client.get("/ui/plugins/BAD SCOPE!").status_code == 404


def test_new_redirects(ui_client):
    r = ui_client.get("/ui/plugins/new?scope=Example.org", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/plugins/example.org/edit"
    r = ui_client.get("/ui/plugins/new?scope=bad!", follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_delete_scope_and_version(ui_client):
    c, store = ui_client, ui_client.app.state.store
    src = _src(c)
    _post(c, "/ui/plugins/example.org/submit", source=src, notes="first")
    _post(c, "/ui/plugins/example.org/promote", version=1)
    _post(c, "/ui/plugins/example.org/submit", source=src + "\n# v2\n")

    # UI template assertions
    index_page = c.get("/ui/plugins").text
    assert '/ui/plugins/example.org/delete' in index_page
    assert '/ui/plugins/_default/delete' not in index_page

    scope_page = c.get("/ui/plugins/example.org").text
    assert 'Delete scope' in scope_page
    assert '/ui/plugins/example.org/v2/delete' in scope_page

    # CSRF check
    assert c.post("/ui/plugins/example.org/delete", follow_redirects=False).status_code == 403
    assert c.post("/ui/plugins/example.org/v2/delete", follow_redirects=False).status_code == 403

    # Delete _default forbidden
    r = _post(c, "/ui/plugins/_default/delete")
    assert r.status_code == 400

    # Delete invalid scope 404
    r = _post(c, "/ui/plugins/bad%20scope/delete")
    assert r.status_code == 404

    # Delete active version fails with error redirect
    r = _post(c, "/ui/plugins/example.org/v1/delete")
    assert r.status_code == 303 and "err=" in r.headers["location"]

    # Delete candidate v2 succeeds
    r = _post(c, "/ui/plugins/example.org/v2/delete")
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert [p.version for p in store.list("example.org")] == [1]

    # Delete entire scope succeeds
    r = _post(c, "/ui/plugins/example.org/delete")
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert store.list("example.org") == []
