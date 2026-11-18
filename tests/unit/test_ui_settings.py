import httpx
import yaml


def _path(c):
    return c.app.state.config_file.path


def _post(c, url, **data):
    return c.post(url, data={"csrf": c.csrf, **data}, follow_redirects=False)


def _read(c):
    return yaml.safe_load(_path(c).read_text())


def test_defaults_and_add_profile(ui_client):
    page = ui_client.get("/ui/settings")
    assert page.status_code == 200 and "Settings" in page.text
    assert 'name="heal_rounds" value="3"' in page.text
    r = _post(ui_client, "/ui/settings/llm", name="pc", base_url="http://pc:1234/v1")
    assert r.status_code == 303
    data = _read(ui_client)
    assert data["llm"]["profiles"]["pc"]["base_url"] == "http://pc:1234/v1"
    assert data["llm"]["order"] == ["pc"]
    assert _post(ui_client, "/ui/settings/llm", name="pc", base_url="http://x/v1").status_code == 400
    assert _post(ui_client, "/ui/settings/llm", name="Bad Name", base_url="http://x/v1").status_code == 400


def test_secret_kept_and_cleared(ui_client):
    text = "llm:\n  profiles:\n    pc:\n      base_url: http://pc/v1\n      api_key: ${LMS_KEY}\n"
    _post(ui_client, "/ui/settings/raw", text=text)
    form = {"base_url": "http://pc/v1", "api_key": ""}
    assert _post(ui_client, "/ui/settings/llm/pc", **form).status_code == 303
    assert _read(ui_client)["llm"]["profiles"]["pc"]["api_key"] == "${LMS_KEY}"
    assert "from ${LMS_KEY}" in ui_client.get("/ui/settings").text
    _post(ui_client, "/ui/settings/llm/pc", clear_api_key="on", **form)
    assert "api_key" not in _read(ui_client)["llm"]["profiles"]["pc"]
    assert _post(ui_client, "/ui/settings/llm/nope", **form).status_code == 404
    assert _post(ui_client, "/ui/settings/llm/pc/delete").status_code == 303
    assert _read(ui_client)["llm"]["profiles"] == {}


def _budgets(**over):
    form = {
        "auto_attempts": "2",
        "heal_rounds": "3",
        "heal_windows": "5m, 30m",
        "agent_wait": "6h",
        "daily_rounds": "12",
        "trace_keep": "20",
        "cross_replay_max": "5",
        "poll_s": "10",
        "llm_wait_s": "1m",
        "give_up_after": "7d",
        "replay_timeout_s": "60",
        "enabled": "on",
    }
    return {**form, **over}


def test_budgets(ui_client):
    r = _post(ui_client, "/ui/settings/budgets", **_budgets(heal_rounds="0"))
    assert r.status_code == 400 and not _path(ui_client).exists()
    assert _post(ui_client, "/ui/settings/budgets", **_budgets(heal_rounds="x")).status_code == 400
    assert _post(ui_client, "/ui/settings/budgets", **_budgets()).status_code == 303
    assert _read(ui_client)["escalation"]["heal_windows"] == ["5m", "30m"]


def test_budgets_leave_default_durations_unset(ui_client):
    # an empty field keeps the default (shown as placeholder) instead of pinning today's value in the file
    blank = {k: "" for k in ("poll_s", "llm_wait_s", "give_up_after", "replay_timeout_s")}
    assert _post(ui_client, "/ui/settings/budgets", **_budgets(**blank)).status_code == 303
    healer = _read(ui_client)["healer"]
    assert not set(blank) & set(healer)
    page = ui_client.get("/ui/settings").text
    assert 'name="replay_timeout_s" value="" placeholder="2m"' in page


def test_raw_yaml(ui_client):
    r = _post(ui_client, "/ui/settings/raw", text="llm: [1")
    assert r.status_code == 400 and not _path(ui_client).exists()
    assert _post(ui_client, "/ui/settings/raw", text="healer:\n  trace_keep: 5\n").status_code == 303
    assert _post(ui_client, "/ui/settings/raw", text="healer:\n  trace_keep: 6\n").status_code == 303
    assert _path(ui_client).with_name("mendarr.yml.bak").exists()
    bad = _post(ui_client, "/ui/settings/raw", text="healer: [")
    assert bad.status_code == 400 and "healer: [" in bad.text


def test_check(ui_client):
    _post(ui_client, "/ui/settings/llm", name="pc", base_url="http://pc/v1")
    st = ui_client.app.state
    st.llm_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "gemma-4"}]}))
    assert "gemma-4" in _post(ui_client, "/ui/settings/llm/pc/check").text
    st.llm_transport = httpx.MockTransport(lambda r: httpx.Response(500))
    assert "unreachable" in _post(ui_client, "/ui/settings/llm/pc/check").text
    assert "unknown profile" in _post(ui_client, "/ui/settings/llm/zz/check").text


def test_notify_test(ui_client):
    assert "No notification channel" in _post(ui_client, "/ui/settings/notify/test", event="healed").text
    _post(ui_client, "/ui/settings/notify", webhook_url="http://hook.test/x", events=["healed"], discord_ping="@here")
    data = _read(ui_client)
    assert data["notify"]["discord_ping"] == "@here"
    st = ui_client.app.state
    st.notify_transport = httpx.MockTransport(lambda r: httpx.Response(204))
    res = _post(ui_client, "/ui/settings/notify/test", event="healed").text
    assert "webhook" in res and "sent" in res
    assert "switched off" in _post(ui_client, "/ui/settings/notify/test", event="gave_up").text


def test_csrf_required(ui_client):
    r = ui_client.post("/ui/settings/raw", data={"text": ""}, follow_redirects=False)
    assert r.status_code == 403


def test_credentials_card_renders(ui_client):
    r = ui_client.get("/ui/settings")
    assert r.status_code == 200
    assert "Credentials / Dashboard User" in r.text
    assert "MENDARR_UI_USER" in r.text
    assert "MENDARR_UI_PASSWORD" in r.text


def test_cookie_settings(ui_client):
    page = ui_client.get("/ui/settings").text
    assert 'action="/ui/settings/cookies"' in page
    r = _post(ui_client, "/ui/settings/cookies", refresh_interval="6h", refresh_idle_days="3")
    assert r.status_code == 303
    assert _read(ui_client)["cookies"] == {"refresh_interval": "6h", "refresh_idle_days": 3.0}
    assert 'name="refresh_interval" value="6h"' in ui_client.get("/ui/settings").text
    _post(ui_client, "/ui/settings/cookies", refresh_interval="", refresh_idle_days="")
    assert _read(ui_client)["cookies"] == {}
    assert _post(ui_client, "/ui/settings/cookies", refresh_idle_days="x").status_code == 400
    assert _post(ui_client, "/ui/settings/cookies", refresh_idle_days="-1").status_code == 400
    assert _post(ui_client, "/ui/settings/cookies", refresh_interval="soon").status_code == 400
    _post(ui_client, "/ui/settings/cookies", refresh_timeout="90s")
    assert _read(ui_client)["cookies"] == {"refresh_timeout": "90s"}
    assert 'name="refresh_timeout" value="90s"' in ui_client.get("/ui/settings").text
    assert _post(ui_client, "/ui/settings/cookies", refresh_timeout="5s").status_code == 400


def test_solver_settings(ui_client):
    assert 'action="/ui/settings/solver"' in ui_client.get("/ui/settings").text
    assert _post(ui_client, "/ui/settings/solver", default_backend="patchright").status_code == 303
    assert _read(ui_client)["solver"] == {"default_backend": "patchright"}
    assert '<option value="patchright" selected>' in ui_client.get("/ui/settings").text
    _post(ui_client, "/ui/settings/solver", default_backend="")
    assert _read(ui_client)["solver"] == {}
    assert _post(ui_client, "/ui/settings/solver", default_backend="chrome").status_code == 400


def test_solver_locale_setting(ui_client):
    assert _post(ui_client, "/ui/settings/solver", default_backend="", locale=" en-US ").status_code == 303
    assert _read(ui_client)["solver"] == {"locale": "en-US"}
    assert 'value="en-US"' in ui_client.get("/ui/settings").text
    assert _post(ui_client, "/ui/settings/solver", default_backend="", locale="en_US").status_code == 400
    assert _read(ui_client)["solver"] == {"locale": "en-US"}
    _post(ui_client, "/ui/settings/solver", default_backend="", locale="")
    assert _read(ui_client)["solver"] == {}


def test_solver_backend_order(ui_client):
    def save(**order):
        return _post(ui_client, "/ui/settings/solver", default_backend="", **order)

    assert save(order_1="obscura", order_2="", order_3="camoufox").status_code == 303
    assert _read(ui_client)["solver"] == {"backend_order": ["obscura", "camoufox"]}
    page = ui_client.get("/ui/settings").text
    assert 'name="order_1" value="obscura"' in page
    assert 'name="order_2" value="camoufox"' in page
    assert 'name="order_3" value=""' in page
    assert 'data-order="obscura,camoufox"' in page
    assert save(order_1="camoufox", order_2="camoufox").status_code == 400
    assert save(order_1="chrome").status_code == 400
    assert _read(ui_client)["solver"] == {"backend_order": ["obscura", "camoufox"]}
    assert save(order_1="camoufox", order_2="patchright", order_3="obscura").status_code == 303
    assert _read(ui_client)["solver"] == {}
    save(order_1="patchright")
    assert _read(ui_client)["solver"] == {"backend_order": ["patchright"]}
    save()
    assert _read(ui_client)["solver"] == {}


# --- Settings → Browser backends ---


def _backend(**over):
    from mendarr.rpc import BackendStatus

    return BackendStatus(**{"name": "obscura", "component": "binary", "installed": "v1", "updatable": True, **over})


def _state(*backends):
    from mendarr.rpc import BackendsState

    return BackendsState(auto_update=True, interval_s=86400, backends=list(backends))


def _fake_backends(worker, **kw):
    """Give the shared fake worker the backend calls (answers come from `kw`; an Exception is raised)."""
    calls: list[tuple] = []

    def make(name, *pre):
        async def call(*args, **kwargs):
            calls.append((name, *args, *kwargs.values()))
            out = kw[name]
            if isinstance(out, Exception):
                raise out
            return out

        return call

    for n in ("backends", "backends_check", "backend_update", "backend_revert", "backends_restart"):
        setattr(worker, n, make(n))
    return calls


def test_backends_card_without_a_view(ui_client):
    r = ui_client.get("/ui/settings")  # the fake worker has no backends(): rendered as not checked
    assert r.status_code == 200
    assert 'id="backends"' in r.text and "Not checked yet" in r.text


def test_backends_loaded_on_page_get(ui_client, fake_worker):
    from mendarr.api.worker_client import WorkerUnavailable

    _fake_backends(fake_worker, backends=WorkerUnavailable("down"))
    assert "Not checked yet" in ui_client.get("/ui/settings").text
    calls = _fake_backends(fake_worker, backends=_state(_backend()))
    page = ui_client.get("/ui/settings").text
    assert "Auto update: on" in page and ui_client.app.state.backends_view is not None
    ui_client.get("/ui/settings")
    assert len(calls) == 1  # fetched once; later GETs use the stored view


def test_backends_check_stores_view_and_renders(ui_client, fake_worker):
    _fake_backends(fake_worker, backends_check=_state(_backend(latest="v2"), _backend(name="patchright", updatable=False, note="pinned")))
    r = _post(ui_client, "/ui/settings/backends/check")
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert ui_client.app.state.backends_view.backends[0].latest == "v2"
    page = ui_client.get("/ui/settings").text
    assert "/ui/settings/backends/obscura/update" in page and "pinned" in page and "Auto update: on" in page


def test_backends_check_worker_unreachable_renders_error(ui_client, fake_worker):
    from mendarr.api.worker_client import WorkerRejected, WorkerUnavailable

    _fake_backends(fake_worker, backends_check=WorkerUnavailable("connection refused"))
    r = _post(ui_client, "/ui/settings/backends/check")
    assert r.status_code == 303 and "err=" in r.headers["location"] and "restarting" in r.headers["location"]
    _fake_backends(fake_worker, backends_check=WorkerRejected(404, "worker answered 404"))  # an old worker
    r = _post(ui_client, "/ui/settings/backends/check")
    assert "err=" in r.headers["location"]
    assert ui_client.get("/ui/settings").status_code == 200


def test_backend_update_failed_shows_error(ui_client, fake_worker):
    _fake_backends(
        fake_worker,
        backend_update=_backend(state="failed", error="sha256 mismatch"),
        backends=_state(_backend(state="failed", error="sha256 mismatch")),
    )
    r = _post(ui_client, "/ui/settings/backends/obscura/update")
    assert "err=" in r.headers["location"] and "sha256" in r.headers["location"]
    assert "sha256 mismatch" in ui_client.get("/ui/settings").text


def test_backend_update_ok_and_pending_restart(ui_client, fake_worker):
    _fake_backends(
        fake_worker,
        backend_update=_backend(installed="v2", latest="v2"),
        backends=_state(_backend(installed="v2")),
    )
    r = _post(ui_client, "/ui/settings/backends/obscura/update")
    assert "ok=" in r.headers["location"] and "v2" in r.headers["location"]
    cam = _backend(name="camoufox", component="browser build", state="pending_restart", installed="1.0", latest="2.0")
    _fake_backends(fake_worker, backend_update=cam, backends=_state(cam))
    r = _post(ui_client, "/ui/settings/backends/camoufox/update")
    assert "ok=" in r.headers["location"] and "restarts" in r.headers["location"]
    assert "Restart now" in ui_client.get("/ui/settings").text


def test_backend_unknown_name_rejected(ui_client, fake_worker):
    calls = _fake_backends(fake_worker)
    for action in ("update", "revert"):
        r = _post(ui_client, f"/ui/settings/backends/nope/{action}")
        assert "err=" in r.headers["location"]
    assert calls == []


def test_backend_revert(ui_client, fake_worker):
    _fake_backends(fake_worker, backend_revert=_backend(installed=""), backends=_state(_backend()))
    r = _post(ui_client, "/ui/settings/backends/obscura/revert")
    assert "ok=" in r.headers["location"] and "built-in" in r.headers["location"]


def test_backends_restart_posts_force(ui_client, fake_worker):
    from mendarr.api.worker_client import WorkerRejected

    from mendarr.rpc import BackendRestartResult

    calls = _fake_backends(fake_worker, backends_restart=BackendRestartResult(closed=3, backends=[_backend()]))
    r = _post(ui_client, "/ui/settings/backends/restart")
    assert "ok=" in r.headers["location"] and "closed%203" in r.headers["location"]
    assert ("backends_restart", True) in calls
    _fake_backends(fake_worker, backends_restart=WorkerRejected(409, "no", "no restart pending"))
    r = _post(ui_client, "/ui/settings/backends/restart")
    assert "err=" in r.headers["location"] and "pending" in r.headers["location"]


def test_backends_routes_need_csrf(ui_client):
    assert ui_client.post("/ui/settings/backends/check", data={}, follow_redirects=False).status_code in (400, 403)
