from mendarr.config import Settings


def test_defaults(monkeypatch):
    for k in ("HOST", "PORT", "WORKER_URL", "SLOTS", "DATA_DIR", "LOG_LEVEL", "PROXY_URL", "PROXY_TYPE"):
        monkeypatch.delenv(f"MENDARR_{k}", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.delenv("PROXY_TYPE", raising=False)
    s = Settings()
    assert (s.host, s.port, s.slots, s.data_dir) == ("0.0.0.0", 8191, 2, "/data")
    assert s.worker_url == "http://mendarr-browser:8192"
    assert s.upstream is None


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MENDARR_PORT", "9000")
    monkeypatch.setenv("MENDARR_SLOTS", "4")
    monkeypatch.setenv("MENDARR_WORKER_URL", "http://worker.lan:8192")
    monkeypatch.setenv("MENDARR_DATA_DIR", "/tmp/x")
    s = Settings()
    assert s.port == 9000
    assert s.slots == 4
    assert s.worker_url == "http://worker.lan:8192"
    assert s.data_dir == "/tmp/x"


def clear_proxy_env(monkeypatch):
    for k in ("URL", "TYPE", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"PROXY_{k}", raising=False)
        monkeypatch.delenv(f"MENDARR_PROXY_{k}", raising=False)


def test_bare_proxy_env(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "192.168.1.5:9999")
    monkeypatch.setenv("PROXY_TYPE", "socks5")
    assert str(Settings().upstream) == "socks5://192.168.1.5:9999"


def test_prefixed_proxy_env_wins(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "a:1")
    monkeypatch.setenv("MENDARR_PROXY_URL", "b:2")
    assert str(Settings().upstream) == "http://b:2"


def test_proxy_by_name(monkeypatch):
    clear_proxy_env(monkeypatch)
    assert str(Settings(proxy_url="h:1", proxy_type="https").upstream) == "https://h:1"


def test_proxy_credentials_env(monkeypatch):
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("MENDARR_PROXY_URL", "h:3128")
    monkeypatch.setenv("MENDARR_PROXY_USERNAME", "u")
    monkeypatch.setenv("MENDARR_PROXY_PASSWORD", "pw")
    up = Settings().upstream
    assert (up.username, up.password) == ("u", "pw")


def test_cookie_ttl_auto_and_refresh_defaults(monkeypatch):
    for k in (
        "COOKIE_MAX_TTL_HOURS",
        "COOKIE_REFRESH_INTERVAL",
        "COOKIE_REFRESH_IDLE_DAYS",
    ):
        monkeypatch.delenv(f"MENDARR_{k}", raising=False)
    s = Settings()
    assert (
        s.cookie_max_ttl_hours,
        s.cookie_refresh_interval,
        s.cookie_refresh_idle_days,
    ) == (6.0, "", 7.0)
    for raw, want in (("auto", 0), ("AUTO", 0), ("0", 0), ("12", 12)):
        monkeypatch.setenv("MENDARR_COOKIE_MAX_TTL_HOURS", raw)
        assert Settings().cookie_max_ttl_hours == want
