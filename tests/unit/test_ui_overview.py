import time

from mendarr.rpc import Cookie


def test_overview_and_cookies(ui_client):
    st = ui_client.app.state
    for ok in (True, True, False):
        st.stats.add("example.org", ok)
    st.cache.put(
        "example.org",
        "direct",
        "UA",
        [Cookie(name="cf_clearance", value="SECRETVALUE", domain="example.org", expires=time.time() + 3600)],
    )
    iid = st.store.note_failure(
        "example.org", kind="challenge_loop", url="https://example.org/", trace_id="", plugin="_default@v1"
    )
    page = ui_client.get("/ui").text
    for needle in ("example.org", "66%", "_default@v1", "(fallback)", f"/ui/incidents/{iid}"):
        assert needle in page
    cookies = ui_client.get("/ui/cookies").text
    assert "cf_clearance" in cookies and "SECRETVALUE" not in cookies
    r = ui_client.post(
        "/ui/cookies/purge", data={"domain": "example.org", "csrf": ui_client.csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and st.cache.entries() == []


def test_egress_card(ui_client):
    r = ui_client.get("/ui/egress")
    assert r.status_code == 200
    assert "direct" in r.text
    assert 'hx-get="/ui/egress"' in ui_client.get("/ui").text

    # with country
    ui_client.app.state.worker.egress_country = "RO"
    r = ui_client.get("/ui/egress")
    assert r.status_code == 200
    assert "RO" in r.text
    assert "🇷🇴" in r.text
