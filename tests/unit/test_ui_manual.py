import time

import pytest

from mendarr.core.manual import ManualError, ManualSession


def sess(status="running", sid="m1"):
    now = time.time()
    return ManualSession(
        id=sid,
        incident_id=7,
        domain="example.org",
        url="https://example.org/x",
        slot=1,
        started=now,
        deadline=now + 600,
        status=status,
        events=4,
    )


class StubManager:
    def __init__(self):
        self.items = {"m1": sess(), "m2": sess("solved", "m2")}
        self.error: ManualError | None = None
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.autosolved: list[str] = []

    async def start(self, iid):
        if self.error:
            raise self.error
        return self.items["m1"]

    async def start_adhoc(self, url, backend="camoufox"):
        if self.error:
            raise self.error
        s = sess(sid="m3")
        s.incident_id = None
        s.url = url
        s.backend = backend
        self.items["m3"] = s
        return s

    def get(self, sid):
        return self.items.get(sid)

    def sessions(self):
        return list(self.items.values())

    def clear_finished(self):
        to_remove = [sid for sid, s in self.items.items() if not s.active]
        for sid in to_remove:
            del self.items[sid]
        return len(to_remove)

    def active_for_slot(self, slot):
        return next((s for s in self.items.values() if s.active and s.slot == slot), None)

    async def cancel(self, sid):
        self.cancelled.append(sid)

    async def finish(self, sid):
        self.finished.append(sid)

    async def autosolve(self, sid):
        if self.error:
            raise self.error
        self.autosolved.append(sid)


@pytest.fixture
def stub(ui_client):
    m = StubManager()
    ui_client.app.state.manual = m
    return m


def post(c, url, data=None, **kw):
    payload = {"csrf": c.csrf}
    if data:
        payload.update(data)
    return c.post(url, data=payload, follow_redirects=False, **kw)


def test_start_redirects(ui_client, stub):
    r = post(ui_client, "/ui/incidents/7/manual")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/manual/m1"


def test_start_htmx(ui_client, stub):
    r = post(ui_client, "/ui/incidents/7/manual", headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/ui/manual/m1"


def test_start_error(ui_client, stub):
    stub.error = ManualError(409, "no slot")
    r = post(ui_client, "/ui/incidents/7/manual")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/manual?error=no%20slot"


def test_start_needs_csrf(ui_client, stub):
    assert ui_client.post("/ui/incidents/7/manual", follow_redirects=False).status_code == 403


def test_start_adhoc_redirects(ui_client, stub):
    r = post(ui_client, "/ui/manual/start", data={"url": "https://example.org/test", "backend": "patchright"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/manual/m3"
    assert stub.items["m3"].backend == "patchright"
    assert stub.items["m3"].incident_id is None


def test_start_adhoc_error(ui_client, stub):
    stub.error = ManualError(409, "no slot")
    r = post(ui_client, "/ui/manual/start", data={"url": "https://example.org/test"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/manual?error=no%20slot"


def test_list(ui_client, stub):
    r = ui_client.get("/ui/manual?error=boom")
    assert r.status_code == 200
    assert "example.org" in r.text and "boom" in r.text
    assert "/ui/manual/m1" in r.text
    assert "Launch Interactive Studio" in r.text
    assert "/ui/manual/start" in r.text


def test_session_page(ui_client, stub):
    r = ui_client.get("/ui/manual/m1")
    assert r.status_code == 200
    assert 'data-slot="1"' in r.text
    assert "/ui/static/js/manual.js" in r.text
    assert "/ui/manual/m1/cancel" in r.text
    assert "/ui/manual/m1/finish" in r.text
    assert "Backend:" in r.text


def test_adhoc_session_page_renders_crumbs(ui_client, stub):
    stub.items["m3"] = sess(sid="m3")
    stub.items["m3"].incident_id = None
    stub.items["m3"].backend = "obscura"
    r = ui_client.get("/ui/manual/m3")
    assert r.status_code == 200
    assert "Ad-hoc Studio" in r.text
    assert "<code>obscura</code>" in r.text
    r_list = ui_client.get("/ui/manual")
    assert "Studio" in r_list.text


def test_status_running_and_solved(ui_client, stub):
    r = ui_client.get("/ui/manual/m1/status")
    assert 'hx-trigger="every 2s"' in r.text
    assert "HX-Trigger" not in r.headers
    r = ui_client.get("/ui/manual/m2/status")
    assert r.headers["HX-Trigger"] == "manual-ended"
    assert "hx-trigger" not in r.text
    assert "cookies cached" in r.text


def test_cancel(ui_client, stub):
    r = post(ui_client, "/ui/manual/m1/cancel")
    assert r.status_code == 303
    assert stub.cancelled == ["m1"]


def test_finish(ui_client, stub):
    r = post(ui_client, "/ui/manual/m1/finish")
    assert r.status_code == 303
    assert stub.finished == ["m1"]


def test_unknown_sid(ui_client, stub):
    assert ui_client.get("/ui/manual/nope").status_code == 404
    assert ui_client.get("/ui/manual/nope/status").status_code == 404
    assert post(ui_client, "/ui/manual/nope/cancel").status_code == 404


def test_requires_login(client):
    r = client.get("/ui/manual", follow_redirects=False)
    assert r.status_code != 200


def test_static_assets(ui_client):
    assert ui_client.get("/ui/static/vendor/novnc/core/rfb.js").status_code == 200
    assert ui_client.get("/ui/static/js/manual.js").status_code == 200


def test_clear_finished_sessions(ui_client, stub):
    # m1 is running, m2 is solved
    r = post(ui_client, "/ui/manual/clear")
    assert r.status_code == 303
    assert "/ui/manual?ok=" in r.headers["Location"]
    assert "m2" not in stub.items
    assert "m1" in stub.items


def test_clear_button_renders_when_finished(ui_client, stub):
    # m2 is solved -> button is present
    r = ui_client.get("/ui/manual")
    assert r.status_code == 200
    assert "/ui/manual/clear" in r.text
    # now remove m2 -> no finished sessions, button is not rendered
    stub.items.pop("m2")
    r2 = ui_client.get("/ui/manual")
    assert r2.status_code == 200
    assert "/ui/manual/clear" not in r2.text


def test_autosolve_route(ui_client, stub):
    r = post(ui_client, "/ui/manual/m1/autosolve")
    assert r.status_code == 303 and r.headers["location"] == "/ui/manual/m1"
    assert stub.autosolved == ["m1"]
    assert ui_client.post("/ui/manual/m1/autosolve", follow_redirects=False).status_code == 403
    assert post(ui_client, "/ui/manual/zz/autosolve").status_code == 404
    stub.error = ManualError(409, "an auto solve is already running")
    assert post(ui_client, "/ui/manual/m1/autosolve").status_code == 409


def test_session_page_has_autosolve_button(ui_client, stub):
    assert "/ui/manual/m1/autosolve" in ui_client.get("/ui/manual/m1").text
