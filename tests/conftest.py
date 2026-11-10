import re

import pytest
from fakeworker import FakeWorker
from fastapi.testclient import TestClient

from mendarr.api.app import create_app
from mendarr.config import Settings


@pytest.fixture
def fake_worker() -> FakeWorker:
    return FakeWorker()


@pytest.fixture
def client(tmp_path, fake_worker):
    with TestClient(create_app(Settings(data_dir=str(tmp_path)), worker=fake_worker)) as c:
        yield c


@pytest.fixture
def ui_client(tmp_path, fake_worker, monkeypatch):
    """Logged-in dashboard client; `ui_client.csrf` holds the session's CSRF token."""
    monkeypatch.setattr("mendarr.api.ui._FAIL_DELAY_S", 0)
    settings = Settings(data_dir=str(tmp_path), ui_user="admin", ui_password="admin", healer_enabled=False)
    with TestClient(create_app(settings, worker=fake_worker)) as c:
        r = c.post("/ui/login", data={"user": "admin", "password": "admin", "next": "/ui"}, follow_redirects=False)
        assert r.status_code == 303
        c.csrf = re.search(r'name="csrf-token" content="([0-9a-f]+)"', c.get("/ui").text).group(1)
        yield c
