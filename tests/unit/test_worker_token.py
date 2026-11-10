from fastapi.testclient import TestClient

from mendarr.config import Settings
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.server import create_app


class FakeBrowser:
    def is_connected(self):
        return True


async def fake_launcher(slot):
    async def close():
        pass

    return LaunchedBrowser(FakeBrowser(), "UA-test", close)


def test_token_required_except_health():
    app = create_app(
        Settings(slots=1, egress_check_url="", worker_token="s3cret"),
        launcher=fake_launcher,
        start_displays=False,
    )
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/info").status_code == 401
        assert c.get("/info", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200
