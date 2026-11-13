import json
import re

import httpx
import pytest
import yaml
from fakeworker import FakeWorker
from fastapi.testclient import TestClient

from mendarr import __version__
from mendarr.api.app import create_app
from mendarr.config import Settings
from mendarr.core.updates import UpdateChecker, is_newer, version_key
from mendarr.healer.config import ConfigFile, NotifyConfig, NotifyEvents, update_policy
from mendarr.healer.notify import Notifier

URL = "https://api.github.com/repos/krakerz/mendarr/releases/latest"
NOW = 2_000_000.0
RELEASE_URL = "https://github.com/krakerz/mendarr/releases/tag/v9.9.9"


def release(tag="v9.9.9"):
    return {"tag_name": tag, "html_url": RELEASE_URL, "published_at": "2026-11-13T10:00:00Z"}


class Github:
    """Fake api.github.com: answers with `response` and records every request."""

    def __init__(self, response=None):
        self.requests: list[httpx.Request] = []
        self.response = response or httpx.Response(200, json=release(), headers={"etag": 'W/"abc"'})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make(tmp_path, gh, *, settings=None, yml=None, notifier=None, worker=None):
    s = settings or Settings(data_dir=str(tmp_path), update_check=True)
    cfgfile = tmp_path / "mendarr.yml"
    if yml is not None:
        cfgfile.write_text(yml)
    clock = [NOW]
    c = UpdateChecker(
        s,
        ConfigFile(cfgfile),
        worker or FakeWorker(),
        notifier,
        transport=httpx.MockTransport(gh),
        clock=lambda: clock[0],
    )
    c.clock = clock
    return c


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("0.9.0", "0.8.1", True),
        ("v0.8.1", "0.8.1", False),
        ("0.8.0", "0.8.1", False),
        ("0.8.1.0", "0.8.1", False),
        ("0.10.0", "0.9.9", True),
        ("1.0.0", "1.0.0rc1", True),
        ("1.0.0-rc1", "1.0.0", False),
        ("1.0.0-rc2", "1.0.0-rc1", True),
        ("0.8.2.dev0+gabc", "0.8.1", True),
        ("nonsense", "0.8.1", False),
        ("0.9.0", "unknown", False),
    ],
)
def test_is_newer(latest, current, newer):
    assert is_newer(latest, current) is newer


def test_version_key_rejects_non_versions():
    assert version_key("latest") is None and version_key("") is None
    assert version_key("v1.2")[0] == (1, 2)


async def test_newer_release_found(tmp_path):
    gh = Github()
    c = make(tmp_path, gh)
    v = await c.check_now()
    assert v.status == "update" and v.latest == "9.9.9" and v.url == RELEASE_URL and v.update_available
    assert v.last_check_at == NOW and v.error == ""
    req = gh.requests[0]
    assert str(req.url) == URL
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["user-agent"] == f"mendarr/{__version__}"
    assert "if-none-match" not in req.headers


async def test_same_version_is_current(tmp_path):
    c = make(tmp_path, Github(httpx.Response(200, json=release(f"v{__version__}"))))
    v = await c.check_now()
    assert v.status == "current" and not v.update_available


async def test_errors_are_stored_and_logged_once(tmp_path, caplog):
    gh = Github(httpx.Response(403, json={"message": "rate limit"}))
    c = make(tmp_path, gh)
    with caplog.at_level("WARNING", logger="mendarr.core.updates"):
        v1 = await c.check_now()
        await c.check_now()
    assert v1.status == "error" and "403" in v1.error
    assert len([r for r in caplog.records if "update check failed" in r.getMessage()]) == 1

    gh.response = httpx.ConnectError("no route")
    assert "ConnectError" in (await c.check_now()).error
    gh.response = httpx.Response(200, text="<html>")
    assert (await c.check_now()).status == "error"
    gh.response = httpx.Response(200, json={"tag_name": "nightly"})
    assert "unrecognised" in (await c.check_now()).error
    gh.response = httpx.Response(200, json=release())
    assert (await c.check_now()).status == "update"


async def test_etag_and_304(tmp_path):
    gh = Github()
    c = make(tmp_path, gh)
    await c.check_now()
    gh.response = httpx.Response(304)
    c.clock[0] += 100
    v = await c.check_now()
    assert gh.requests[1].headers["if-none-match"] == 'W/"abc"'
    assert v.status == "update" and v.latest == "9.9.9" and v.last_check_at == NOW + 100


async def test_disabled_makes_no_request(tmp_path):
    gh = Github()
    c = make(tmp_path, gh, settings=Settings(data_dir=str(tmp_path), update_check=False))
    assert (await c.check_now()).status == "never"
    assert await c.tick() == c.POLL_S
    c.clock[0] += 10_000
    await c.tick()
    assert gh.requests == []
    # the YAML wins over the environment, and is re-read
    (tmp_path / "mendarr.yml").write_text("updates:\n  check: true\n")
    await c.tick()
    c.clock[0] += 61
    await c.tick()
    assert len(gh.requests) == 1


async def test_tick_first_check_after_60s_then_every_interval(tmp_path):
    gh = Github()
    c = make(tmp_path, gh, yml="updates:\n  interval: 2h\n")
    await c.tick()
    assert gh.requests == []
    c.clock[0] += 61
    await c.tick()
    assert len(gh.requests) == 1
    c.clock[0] += 3600
    await c.tick()
    assert len(gh.requests) == 1
    c.clock[0] += 3600
    await c.tick()
    assert len(gh.requests) == 2


def test_policy_clamps_interval(tmp_path):
    s = Settings(data_dir=str(tmp_path), update_check=True, update_check_interval="5m")
    cfg = ConfigFile(tmp_path / "nope.yml").get()
    assert update_policy(s, cfg) == (True, 3600.0, 300.0)
    cfg.updates.interval = 43200
    cfg.updates.check = False
    assert update_policy(s, cfg) == (False, 43200.0, 43200.0)
    day = Settings(data_dir=str(tmp_path), update_check=True, update_check_interval="1d")
    assert update_policy(day, ConfigFile(tmp_path / "x").get())[1] == 86400


async def test_state_survives_new_instance(tmp_path):
    c = make(tmp_path, Github())
    await c.check_now()
    gh2 = Github(httpx.Response(304))
    c2 = make(tmp_path, gh2)
    v = c2.snapshot()
    assert v.latest == "9.9.9" and v.last_check_at == NOW and v.status == "update"
    await c2.check_now()
    assert gh2.requests[0].headers["if-none-match"] == 'W/"abc"'
    assert json.loads((tmp_path / "updates.json").read_text())["latest"] == "9.9.9"


async def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / "updates.json").write_text("{not json")
    assert make(tmp_path, Github()).snapshot().status == "never"


def _notifier(seen):
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(204)

    return Notifier(transport=httpx.MockTransport(handler))


def _yml(event_on):
    return yaml.safe_dump({"notify": {"webhook_url": "http://hook.test/x", "events": {"update_available": event_on}}})


async def test_notified_once_per_version(tmp_path):
    seen: list = []
    gh = Github()
    c = make(tmp_path, gh, yml=_yml(True), notifier=_notifier(seen))
    await c.check_now()
    await c.check_now()
    assert len(seen) == 1
    assert seen[0]["event"] == "update_available" and "9.9.9" in seen[0]["message"] and seen[0]["url"] == RELEASE_URL
    # a restart does not repeat it
    c2 = make(tmp_path, gh, notifier=_notifier(seen))
    await c2.check_now()
    assert len(seen) == 1
    # a newer version does
    gh.response = httpx.Response(200, json=release("v10.0.0"))
    await c2.check_now()
    assert len(seen) == 2 and "10.0.0" in seen[1]["message"]


async def test_no_notification_unless_event_enabled(tmp_path):
    seen: list = []
    c = make(tmp_path, Github(), yml=_yml(False), notifier=_notifier(seen))
    await c.check_now()
    assert seen == []
    assert NotifyEvents().update_available is False and NotifyConfig().events.update_available is False
    # enabling it later notifies for the version still pending
    (tmp_path / "mendarr.yml").write_text(_yml(True))
    await c.check_now()
    assert len(seen) == 1


async def test_no_notification_when_not_newer(tmp_path):
    seen: list = []
    gh = Github(httpx.Response(200, json=release(f"v{__version__}")))
    c = make(tmp_path, gh, yml=_yml(True), notifier=_notifier(seen))
    await c.check_now()
    assert seen == []


async def test_worker_mismatch(tmp_path):
    w = FakeWorker()
    c = make(tmp_path, Github(), worker=w)
    assert not (await c.view()).worker_mismatch
    w.version = "0.1.0"
    assert not (await c.view()).worker_mismatch  # cached for 5 minutes
    c.clock[0] += 301
    v = await c.view()
    assert v.worker_mismatch and v.worker_version == "0.1.0"
    w.version = __version__
    assert not (await c.check_now()).worker_mismatch  # a check always asks


async def test_worker_down_is_not_a_mismatch(tmp_path):
    class Down(FakeWorker):
        async def info(self):
            raise OSError("down")

    v = await make(tmp_path, Github(), worker=Down()).view()
    assert v.worker_version == "" and not v.worker_mismatch


# --- dashboard ---


@pytest.fixture
def upd_client(tmp_path, monkeypatch):
    """Logged-in dashboard with the update checker on and a fake GitHub."""
    monkeypatch.setattr("mendarr.api.ui._FAIL_DELAY_S", 0)
    gh = Github()
    worker = FakeWorker()
    s = Settings(data_dir=str(tmp_path), ui_user="admin", ui_password="admin", healer_enabled=False, update_check=True)
    with TestClient(create_app(s, worker=worker, update_transport=httpx.MockTransport(gh))) as c:
        c.post("/ui/login", data={"user": "admin", "password": "admin", "next": "/ui"}, follow_redirects=False)
        c.csrf = re.search(r'name="csrf-token" content="([0-9a-f]+)"', c.get("/ui").text).group(1)
        c.gh, c.worker = gh, worker
        yield c


def _post(c, url, **data):
    return c.post(url, data={"csrf": c.csrf, **data}, follow_redirects=False)


def test_fragment_empty_before_any_check(upd_client):
    r = upd_client.get("/ui/updates")
    assert r.status_code == 200 and "New version" not in r.text and "Browser worker" not in r.text
    assert upd_client.gh.requests == []  # the fragment never asks GitHub


def test_fragment_card_and_check_now(upd_client):
    r = _post(upd_client, "/ui/settings/updates/check")
    assert r.status_code == 303 and "9.9.9" in r.headers["location"]
    html = upd_client.get("/ui/updates").text
    assert 'data-update-card="9.9.9"' in html and "New version available" in html
    assert f'href="{RELEASE_URL}"' in html and 'rel="noopener noreferrer"' in html and "Dismiss" in html
    n = len(upd_client.gh.requests)
    upd_client.get("/ui/updates")
    assert len(upd_client.gh.requests) == n
    page = upd_client.get("/ui/settings").text
    assert "New version 9.9.9 available" in page and 'id="update-slot"' in page


def test_fragment_nothing_when_up_to_date(upd_client):
    upd_client.gh.response = httpx.Response(200, json=release(f"v{__version__}"))
    _post(upd_client, "/ui/settings/updates/check")
    assert "New version" not in upd_client.get("/ui/updates").text
    assert "Up to date" in upd_client.get("/ui/settings").text


def test_fragment_worker_mismatch(upd_client):
    upd_client.worker.version = "0.0.1"
    html = upd_client.get("/ui/updates").text
    assert "Browser worker is on" in html and "0.0.1" in html and __version__ in html
    assert "data-update-dismiss" not in html


def test_check_now_error_and_csrf(upd_client):
    upd_client.gh.response = httpx.Response(403)
    r = _post(upd_client, "/ui/settings/updates/check")
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert "Error:" in upd_client.get("/ui/settings").text
    n = len(upd_client.gh.requests)
    bad = upd_client.post("/ui/settings/updates/check", data={"csrf": "wrong"}, follow_redirects=False)
    assert bad.status_code in (400, 403) and len(upd_client.gh.requests) == n


def test_settings_save(upd_client, tmp_path):
    r = _post(upd_client, "/ui/settings/updates", interval="12h")  # no checkbox = off
    assert r.status_code == 303
    data = yaml.safe_load((tmp_path / "mendarr.yml").read_text())
    assert data["updates"] == {"check": False, "interval": "12h"}
    page = upd_client.get("/ui/settings").text
    assert 'name="interval" value="12h"' in page and "disabled" in page
    assert "err=" in _post(upd_client, "/ui/settings/updates/check").headers["location"]
    assert _post(upd_client, "/ui/settings/updates", check="on", interval="5m").status_code == 400
    assert _post(upd_client, "/ui/settings/updates", check="on", interval="soon").status_code == 400
    assert _post(upd_client, "/ui/settings/updates", check="on", interval="1d").status_code == 303
    assert yaml.safe_load((tmp_path / "mendarr.yml").read_text())["updates"] == {"check": True, "interval": "1d"}
    assert upd_client.gh.requests == []
