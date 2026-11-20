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


DAY = 86400


def _aged(st, domain, days, *, ok=True):
    """Record a solve for `domain` as if it happened `days` ago."""
    real = st.stats._clock
    st.stats._clock = lambda: time.time() - days * DAY
    try:
        st.stats.add(domain, ok)
    finally:
        st.stats._clock = real


def _last_aged(st, domain, outcome, days):
    real = st.stats._clock
    st.stats._clock = lambda: time.time() - days * DAY
    try:
        st.stats.set_last(domain, outcome)
    finally:
        st.stats._clock = real


def _cached(st, domain, days):
    st.cache.put(
        domain,
        "direct",
        "UA",
        [Cookie(name="cf_clearance", value="v", domain=domain, expires=time.time() + 3600)],
        last_used=time.time() - days * DAY,
    )


def _set_days(c, value):
    r = c.post("/ui/settings/ui", data={"csrf": c.csrf, "hide_unused_days": str(value)}, follow_redirects=False)
    assert r.status_code == 303


def test_overview_hides_unused_domains(ui_client):
    st = ui_client.app.state
    _aged(st, "old-solve.org", 5)
    _aged(st, "new-solve.org", 1)
    _last_aged(st, "old-quiet.org", "no_challenge", 5)
    _last_aged(st, "new-quiet.org", "no_challenge", 1)
    _cached(st, "old-cache.org", 5)
    _cached(st, "new-cache.org", 1)
    # a cache-served request: old solve, fresh cache touch only
    _aged(st, "cache-served.org", 6)
    _cached(st, "cache-served.org", 0)
    # plugin scope only: no timestamp at all
    st.store.note_failure("inc.org", kind="challenge_loop", url="https://inc.org/", trace_id="", plugin="_default@v1")
    _aged(st, "inc.org", 6)
    page = ui_client.get("/ui").text
    for shown in ("new-solve.org", "new-quiet.org", "new-cache.org", "cache-served.org", "inc.org"):
        assert shown in page
    for hidden in ("old-solve.org", "old-quiet.org", "old-cache.org"):
        assert hidden not in page
    assert "3 domains unused for 3+ days are hidden" in page
    assert 'href="/ui?all=1"' in page
    # the stat card counts every domain
    assert '<div class="stat-value">8 ' in page and "8 domains" in page

    full = ui_client.get("/ui?all=1").text
    for d in ("old-solve.org", "old-quiet.org", "old-cache.org", "new-solve.org"):
        assert d in full
    assert "Showing all domains" in full and 'href="/ui"' in full and "are hidden" not in full
    assert 'hx-get="/ui?all=1"' in full and 'hx-get="/ui"' in page


def test_overview_hide_singular_and_all_hidden(ui_client):
    st = ui_client.app.state
    _aged(st, "old-solve.org", 5)
    page = ui_client.get("/ui").text
    assert "1 domain unused for 3+ days is hidden" in page
    assert "No traffic yet" not in page and "old-solve.org" not in page
    assert "No traffic yet" in _fresh_empty(ui_client)


def _fresh_empty(c):
    st = c.app.state
    st.stats._conn.execute("DELETE FROM domain_runs")
    st.stats._conn.commit()
    return c.get("/ui").text


def test_overview_hide_disabled_and_custom_days(ui_client):
    st = ui_client.app.state
    _aged(st, "old-solve.org", 5)
    _set_days(ui_client, 0)
    page = ui_client.get("/ui").text
    assert "old-solve.org" in page and "are hidden" not in page and "Showing all" not in page
    _set_days(ui_client, 10)
    assert "old-solve.org" in ui_client.get("/ui").text
    _set_days(ui_client, 2)
    page = ui_client.get("/ui").text
    assert "old-solve.org" not in page and "unused for 2+ days" in page


def test_ui_settings_hide_unused_days(ui_client):
    import yaml

    page = ui_client.get("/ui/settings").text
    assert 'action="/ui/settings/ui"' in page and 'placeholder="3"' in page
    path = ui_client.app.state.config_file.path

    def read():
        return yaml.safe_load(path.read_text())

    _set_days(ui_client, 7)
    assert read()["ui"] == {"hide_unused_days": 7}
    _set_days(ui_client, "")
    assert "ui" not in read()  # no empty `ui: {}` left behind
    for bad in ("x", "-1", "366", "1.5"):
        r = ui_client.post("/ui/settings/ui", data={"csrf": ui_client.csrf, "hide_unused_days": bad})
        assert r.status_code == 400, bad
