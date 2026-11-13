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


def test_active_solves_empty_and_populated(ui_client):
    st = ui_client.app.state
    # Initially empty
    r = ui_client.get("/ui/active")
    assert r.status_code == 200
    assert "In progress" not in r.text  # hidden / collapsed when idle
    assert 'id="active-solves"' in r.text

    # Overview page also has active solves container
    page = ui_client.get("/ui").text
    assert 'id="active-solves"' in page
    assert 'hx-get="/ui/active"' in page
    assert "In progress" in page  # stat card label

    # Add an active solve
    tok = st.tracker.start(
        "solve", "active-site.com", "https://active-site.com/search", description="plugin: active-site.com:1"
    )
    # Add an active healer item
    tok2 = st.tracker.start(
        "healer", "broken-site.com", "https://broken-site.com/", description="round 1 via lm-studio", incident_id=99
    )

    r = ui_client.get("/ui/active")
    assert r.status_code == 200
    assert "active-site.com" in r.text
    assert "broken-site.com" in r.text
    assert "plugin: active-site.com:1" in r.text
    assert "round 1 via lm-studio" in r.text
    assert "/ui/incidents/99" in r.text
    assert "badge-solve" in r.text
    assert "badge-healer" in r.text

    # Overview also shows active items
    overview_page = ui_client.get("/ui").text
    assert "active-site.com" in overview_page
    assert "broken-site.com" in overview_page

    # Cleanup
    st.tracker.finish(tok)
    st.tracker.finish(tok2)
    assert st.tracker.count() == 0
    r_after = ui_client.get("/ui/active")
    assert "active-site.com" not in r_after.text


def test_no_challenge_row_and_mismatch(ui_client):
    st = ui_client.app.state
    st.stats.set_last("quiet.example", "no_challenge")
    st.stats.set_last("busy.example", "solved")
    st.stats.bump("quiet.example", "check_mismatch")
    st.stats.bump("quiet.example", "check_mismatch")
    page = ui_client.get("/ui").text
    assert "quiet.example" in page and "no challenge" in page
    assert "check≠browser ×2" in page
    assert "busy.example" not in page


def test_egress_card_stale(ui_client, monkeypatch):
    from mendarr.rpc import EgressInfo

    async def stale():
        return EgressInfo(ip="1.2.3.4", checked_at=time.time() - 120, stale=True)

    monkeypatch.setattr(ui_client.app.state.worker, "egress", stale)
    r = ui_client.get("/ui/egress")
    assert "stale" in r.text and "1.2.3.4" in r.text


def test_cookies_page_per_cookie_expiry_and_checks(ui_client):
    st = ui_client.app.state
    now = time.time()
    st.cache.put(
        "example.org",
        "direct",
        "UA",
        [
            Cookie(name="cf_clearance", value="SECRETVALUE", domain="example.org", expires=now + 3600),
            Cookie(name="sessid", value="SESSVALUE", domain="example.org", expires=0),
        ],
    )
    page = ui_client.get("/ui/cookies").text
    assert "cf_clearance" in page and "sessid" in page and "session" in page and "sets lifetime" in page
    assert "SECRETVALUE" not in page and "SESSVALUE" not in page
    assert "Last check" in page and "Next check" in page
    st.cache.set_check("example.org", "solved")
    assert "solved" in ui_client.get("/ui/cookies").text
