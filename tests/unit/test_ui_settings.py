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
