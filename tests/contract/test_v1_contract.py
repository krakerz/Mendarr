"""Contract tests for /v1; cases derived from FlareSolverr src/tests.py (MIT)."""

from fakeworker import challenge_result, plain_result, snapshot

from mendarr.rpc import CheckResult, SolveResult, WorkerError

URL = "http://example.com/page"
COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "size", "httpOnly", "secure", "session", "sameSite"}


def post(client, body=None, **kw):
    r = client.post("/v1", json=body, **kw) if body is not None else client.post("/v1", **kw)
    j = r.json()
    assert j["startTimestamp"] > 10000
    assert j["endTimestamp"] >= j["startTimestamp"]
    assert j["version"] == "3.3.21"
    assert "mendarrVersion" in j
    return r, j


def get_req(client, **extra):
    return post(client, {"cmd": "request.get", "url": URL, **extra})


def test_missing_cmd_and_non_json(client):
    for kw in ({"json": {}}, {"content": b"nope"}):
        r = client.post("/v1", **kw)
        j = r.json()
        assert r.status_code == 500 and j["status"] == "error"
        assert j["message"] == "Error: Request parameter 'cmd' is mandatory."
        assert "solution" not in j


def test_wrong_cmd(client):  # test_v1_endpoint_wrong_cmd
    r, j = post(client, {"cmd": "request.bad"})
    assert r.status_code == 500
    assert j["message"] == "Error: Request parameter 'cmd' = 'request.bad' is invalid."


def test_get_without_url(client):
    r, j = post(client, {"cmd": "request.get"})
    assert r.status_code == 500
    assert j["message"] == "Error: Request parameter 'url' is mandatory in 'request.get' command."


def test_get_with_postdata(client):
    r, j = post(client, {"cmd": "request.get", "url": URL, "postData": "a=b"})
    assert j["message"] == "Error: Cannot use 'postBody' when sending a GET request."


def test_post_without_postdata(client):
    r, j = post(client, {"cmd": "request.post", "url": URL})
    assert "Request parameter 'postData' is mandatory in 'request.post' command" in j["message"]


def test_plain_page(client):
    r, j = get_req(client)
    assert r.status_code == 200 and j["status"] == "ok"
    assert j["message"] == "Challenge not detected!"
    sol = j["solution"]
    assert set(sol) == {"url", "status", "userAgent", "turnstile_token", "headers", "response", "cookies"}
    assert sol["headers"] == {} and sol["turnstile_token"] is None
    assert sol["cookies"] and all(COOKIE_KEYS <= set(c) for c in sol["cookies"])


def test_challenge_solved(client, fake_worker):
    fake_worker.solve_results.append(challenge_result(URL))
    _, j = get_req(client)
    assert j["message"] == "Challenge solved!"
    cf = next(c for c in j["solution"]["cookies"] if c["name"] == "cf_clearance")
    assert isinstance(cf["expiry"], int) and cf["session"] is False


def test_return_only_cookies(client):  # test_v1_endpoint_request_get_returnOnlyCookies_param
    _, j = get_req(client, returnOnlyCookies=True)
    assert "headers" not in j["solution"] and "response" not in j["solution"]


def test_return_screenshot(client, fake_worker):
    fake_worker.solve_results.append(plain_result(URL).model_copy(update={"screenshot_b64": "AAAA"}))
    _, j = get_req(client, returnScreenshot=True)
    assert j["solution"]["screenshot"] == "AAAA"
    assert fake_worker.calls_of("solve")[0].screenshot is True


def test_unknown_and_deprecated_params(client):
    r, _ = get_req(
        client, foo=1, headers={"a": "b"}, userAgent="x", download=True, returnRawHtml=True, tabs_till_verify=3
    )
    assert r.status_code == 200


def test_max_timeout_defaults(client, fake_worker):
    for extra in ({}, {"maxTimeout": 0}, {"maxTimeout": "abc"}):
        fake_worker.calls.clear()
        get_req(client, **extra)
        t = fake_worker.calls_of("solve")[0].timeout_s
        assert 59 < t <= 60


def test_deadline_timeout(client, fake_worker):
    fake_worker.solve_results.append(
        SolveResult(snapshot=snapshot(URL), error=WorkerError(type="deadline", detail="late"), user_agent="u")
    )
    r, j = get_req(client, maxTimeout=1000)
    assert r.status_code == 500
    assert j["message"] == "Error: Error solving the challenge. Timeout after 1.0 seconds."
    assert j["mendarrFailure"] == "timeout"


def test_blocked(client, fake_worker):
    fake_worker.solve_results.append(
        SolveResult(
            snapshot=snapshot(
                URL, status=403, title="Attention Required! | Cloudflare",
                html="<title>Attention Required! | Cloudflare</title><p>Sorry, you have been blocked</p>",
                headers={"server": "cloudflare", "cf-ray": "1"},
            ),
            user_agent="u",
        )
    )
    r, j = get_req(client)  # test_v1_endpoint_request_get_cloudflare_blocked
    assert r.status_code == 500
    assert j["message"] == (
        "Error: Error solving the challenge. Cloudflare has blocked this request. "
        "Probably your IP is banned for this site, check in your web browser."
    )


def test_navigation_error(client, fake_worker):
    fake_worker.solve_results.append(
        SolveResult(error=WorkerError(type="navigation", detail="NS_ERROR_UNKNOWN_HOST\nline2"))
    )
    _, j = get_req(client)
    assert j["message"] == "Error: Error solving the challenge. NS_ERROR_UNKNOWN_HOST\\nline2"
    assert j["mendarrFailure"] == "network"


def test_sessions_create(client):
    _, j = post(client, {"cmd": "sessions.create"})
    assert j["message"] == "Session created successfully." and j["session"]
    _, j = post(client, {"cmd": "sessions.create", "session": "abc"})
    assert j["session"] == "abc"
    _, j = post(client, {"cmd": "sessions.create", "session": "abc"})
    assert j["message"] == "Session already exists."


def test_sessions_list(client):
    post(client, {"cmd": "sessions.create", "session": "abc"})
    _, j = post(client, {"cmd": "sessions.list"})
    assert j["message"] == "" and "abc" in j["sessions"]


def test_sessions_destroy(client):
    r, j = post(client, {"cmd": "sessions.destroy", "session": "nope"})
    assert r.status_code == 500 and j["message"] == "Error: The session doesn't exist."
    post(client, {"cmd": "sessions.create", "session": "abc"})
    r, j = post(client, {"cmd": "sessions.destroy", "session": "abc"})
    assert r.status_code == 200 and j["message"] == "The session has been removed."


def test_cookie_cache_reuse(client, fake_worker):
    fake_worker.solve_results.append(challenge_result(URL))
    get_req(client)
    fake_worker.check_results.append(CheckResult(snapshot=snapshot(URL)))
    _, j = get_req(client)
    assert j["message"] == "Challenge not detected!"
    assert j["solution"]["userAgent"] == fake_worker.ua
    assert len(fake_worker.calls_of("solve")) == 1


def test_egress(client):
    r = client.get("/v1/egress")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["ip"] == "203.0.113.7"


def test_post_strips_leading_question_mark(client, fake_worker):
    post(client, {"cmd": "request.post", "url": URL, "postData": "?a=b"})
    assert fake_worker.calls_of("solve")[0].post_data == "a=b"
