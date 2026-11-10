from mendarr import __version__


def test_root(client, fake_worker):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["msg"] == "FlareSolverr is ready!"
    assert body["version"] == "3.3.21"
    assert body["userAgent"] == fake_worker.ua
    assert body["mendarrVersion"] == __version__


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
